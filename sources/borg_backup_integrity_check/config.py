"""AppConfig: the single canonical configuration, read from YunoHost app settings + the secret store.

There is exactly one configuration model. The YunoHost install form and the
config panel write ``/etc/yunohost/apps/<app>/settings.yml``; secrets go to the
:class:`SecretStore`; this module only *reads* them.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import APP_ID
from .errors import ConfigurationError
from .redaction import secret_fingerprint
from .secrets import SecretStore

DEFAULTS: dict[str, Any] = {
    "use_borg_ynh": True,
    "borg_app": "",
    "borg_repository": "",
    "borg_repository_remote": "",
    "borg_ssh_key": "auto",
    "borg_remote_path": "",
    "archive_scheme": "borg_ynh",
    "archive_pattern": "",
    "generation_window_hours": 12,
    "backup_max_age_hours": 36,
    "yunohost_version_policy": "match_backup",
    "cloud_provider": "hetzner",
    "hetzner_location": "fsn1",
    "hetzner_server_type": "auto",
    "digitalocean_region": "fra1",
    "digitalocean_size": "auto",
    "digitalocean_project": "",
    "restore_volume": "auto",
    "vm_min_memory_mb": 4096,
    "vm_ssh_port": 22022,
    "prefer_ipv6": False,
    "restore_mode": "sampled",
    "sample_size": 20,
    "components": "all",
    "borg_check_level": "archives",
    "deep_check": "sampled_dry_run",
    "deep_check_sample": 200,
    "manifest_compare": True,
    "warn_growth_pct": 40,
    "warn_shrink_pct": 20,
    "baseline_mode": "previous_and_rolling",
    "history_retention_days": 400,
    "schedule_enabled": True,
    "schedule_frequency": "daily",
    "schedule_time": "09:00",
    "schedule_weekday": "Mon",
    "report_email": "",
    "report_on": "always",
    "retain_hours": 4,
    "keep_runs": 30,
    "keep_host_on_failure": False,
    "restore_borg_app": True,
    "never_restore_apps": "borg,borgserver,borg-backup-integrity-check",
    "borg_lock_wait": 900,
    "log_level": "info",
    "email_include_sender": True,
}

_BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
_INT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, int) and not isinstance(v, bool)}


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Paths:
    app_id: str = APP_ID
    settings_file: Path = field(
        default_factory=lambda: Path(f"/etc/yunohost/apps/{APP_ID}/settings.yml")
    )
    etc_dir: Path = field(default_factory=lambda: Path(f"/etc/{APP_ID}"))
    data_dir: Path = field(default_factory=lambda: Path(f"/var/lib/{APP_ID}"))
    log_dir: Path = field(default_factory=lambda: Path(f"/var/log/{APP_ID}"))
    install_dir: Path = field(default_factory=lambda: Path(f"/var/www/{APP_ID}"))

    @property
    def secrets_file(self) -> Path:
        return self.etc_dir / "secrets.json"

    @property
    def keys_dir(self) -> Path:
        return self.etc_dir / "keys"

    @property
    def restore_host_key(self) -> Path:
        return self.keys_dir / "restore_host_ed25519"

    @property
    def dedicated_borg_key(self) -> Path:
        return self.keys_dir / "borg_repository_ed25519"

    @property
    def history_dir(self) -> Path:
        return self.data_dir / "history"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @classmethod
    def from_environment(cls, app_id: str | None = None) -> Paths:
        app_id = app_id or os.environ.get("BBIC_APP_ID") or APP_ID
        paths = cls(app_id=app_id)
        paths.settings_file = Path(
            os.environ.get("BBIC_SETTINGS_FILE", f"/etc/yunohost/apps/{app_id}/settings.yml")
        )
        paths.etc_dir = Path(os.environ.get("BBIC_ETC_DIR", f"/etc/{app_id}"))
        paths.data_dir = Path(os.environ.get("BBIC_DATA_DIR", f"/var/lib/{app_id}"))
        paths.log_dir = Path(os.environ.get("BBIC_LOG_DIR", f"/var/log/{app_id}"))
        paths.install_dir = Path(os.environ.get("BBIC_INSTALL_DIR", f"/var/www/{app_id}"))
        return paths


@dataclass
class BorgSource:
    """Resolved Borg access details (after optional borg_ynh discovery)."""

    repository: str
    passphrase: str | None
    binary: str
    ssh_key_path: str | None
    remote_path: str | None
    remote_repository: str  # as reachable from the restore host
    origin: str  # borg_ynh:<app> | manual

    def describe(self) -> str:
        key = self.ssh_key_path or "none"
        pp = "configured" if self.passphrase else "NOT configured"
        return f"repository {self.repository} (source: {self.origin}, binary {self.binary}, key {key}, passphrase {pp})"


class AppConfig:
    def __init__(self, settings: dict[str, Any], secrets: SecretStore, paths: Paths) -> None:
        self.raw = dict(settings)
        self.secrets = secrets
        self.paths = paths

    # ------------------------------------------------------------ loading
    @classmethod
    def load(cls, paths: Paths | None = None) -> AppConfig:
        paths = paths or Paths.from_environment()
        if not paths.settings_file.is_file():
            raise ConfigurationError(
                f"settings file {paths.settings_file} not found: is the app installed?"
            )
        with open(paths.settings_file, encoding="utf-8") as fh:
            settings = yaml.safe_load(fh) or {}
        if settings.get("data_dir"):
            paths.data_dir = Path(settings["data_dir"])
        if settings.get("install_dir"):
            paths.install_dir = Path(settings["install_dir"])
        return cls(settings, SecretStore(paths.secrets_file), paths)

    # ------------------------------------------------------------ typed access
    def get(self, key: str, default: Any = None) -> Any:
        value = self.raw.get(key)
        # Unanswered/invisible install questions are stored as null and may surface as "None".
        if value is None or value == "" or value == "None" or value == "_none":
            value = DEFAULTS.get(key, default) if default is None else default
        if key in _BOOL_KEYS:
            return _to_bool(value)
        if key in _INT_KEYS:
            try:
                return int(value)
            except (TypeError, ValueError):
                return int(DEFAULTS[key])
        return value

    def __getattr__(self, key: str) -> Any:
        if key in DEFAULTS:
            return self.get(key)
        raise AttributeError(key)

    @property
    def app_id(self) -> str:
        return self.paths.app_id

    @property
    def components_list(self) -> list[str]:
        raw = str(self.get("components") or "all")
        items = [c.strip() for c in raw.replace(";", ",").split(",") if c.strip()]
        return items or ["all"]

    @property
    def never_restore(self) -> set[str]:
        raw = str(self.get("never_restore_apps") or "")
        base = {c.strip() for c in raw.split(",") if c.strip()}
        base.add(self.app_id)
        return base

    @property
    def provider_name(self) -> str:
        return str(self.get("cloud_provider"))

    @property
    def provider_settings(self) -> dict[str, Any]:
        return {
            k: self.get(k)
            for k in DEFAULTS
            if k.startswith(("hetzner_", "digitalocean_", "restore_volume", "vm_", "prefer_ipv6"))
        }

    @property
    def owner_id(self) -> str:
        """Stable identifier of this production server for labelling cloud resources."""
        return (
            secret_fingerprint(socket.getfqdn() + "|" + self.app_id)[:12]
            if hasattr(socket, "getfqdn")
            else self.app_id
        )

    def report_recipient(self) -> str:
        return str(self.get("report_email") or "root")

    def on_calendar(self) -> str:
        """Systemd OnCalendar expression derived from the schedule settings."""
        time_ = str(self.get("schedule_time") or "09:00")
        hh, _, mm = time_.partition(":")
        stamp = f"{int(hh):02d}:{int(mm or 0):02d}:00"
        freq = str(self.get("schedule_frequency"))
        if freq == "weekly":
            return f"{self.get('schedule_weekday')} *-*-* {stamp}"
        if freq == "monthly":
            return f"*-*-01 {stamp}"
        return f"*-*-* {stamp}"

    # ------------------------------------------------------------ borg source
    def borg_source(self, discovery: Any | None = None) -> BorgSource:
        """Resolve repository/passphrase/binary/key, reusing borg_ynh when configured."""
        from .yunohost import discover_borg_ynh

        remote_override = str(self.get("borg_repository_remote") or "").strip()
        dedicated_key = (
            str(self.paths.dedicated_borg_key) if self.paths.dedicated_borg_key.is_file() else None
        )
        key_mode = str(self.get("borg_ssh_key") or "auto")

        if self.get("use_borg_ynh"):
            instances = discovery if discovery is not None else discover_borg_ynh()
            wanted = str(self.get("borg_app") or "")
            inst = (
                next((i for i in instances if i.app == wanted), None)
                if wanted
                else (instances[0] if instances else None)
            )
            if inst is None:
                raise ConfigurationError(
                    "use_borg_ynh is enabled but no installed borg_ynh instance was found"
                    + (f" (looked for {wanted!r})" if wanted else "")
                )
            key = (
                inst.ssh_key_path
                if key_mode == "auto" and inst.ssh_key_path
                else dedicated_key or inst.ssh_key_path
            )
            return BorgSource(
                repository=inst.repository,
                passphrase=inst.passphrase,
                binary=inst.binary or _fallback_borg_binary(),
                ssh_key_path=key,
                remote_path=inst.remote_path or (str(self.get("borg_remote_path") or "") or None),
                remote_repository=remote_override or inst.repository,
                origin=f"borg_ynh:{inst.app}",
            )
        repository = str(self.get("borg_repository") or "").strip()
        if not repository:
            raise ConfigurationError("borg_repository is not configured")
        return BorgSource(
            repository=repository,
            passphrase=self.secrets.get("borg_passphrase"),
            binary=_fallback_borg_binary(),
            ssh_key_path=dedicated_key,
            remote_path=str(self.get("borg_remote_path") or "") or None,
            remote_repository=remote_override or repository,
            origin="manual",
        )

    def credentials(self) -> dict[str, str]:
        return self.secrets.all_values()

    def to_public_dict(self) -> dict[str, Any]:
        """Settings without secrets (for status output / debugging)."""
        return {k: self.get(k) for k in DEFAULTS}


def _fallback_borg_binary() -> str:
    from .borg.client import BorgClient

    binary = BorgClient.find_binary([os.environ.get("BBIC_BORG_BINARY", ""), "/usr/bin/borg"])
    if not binary:
        raise ConfigurationError(
            "no borg binary found (install the 'borgbackup' package or use borg_ynh)"
        )
    return binary

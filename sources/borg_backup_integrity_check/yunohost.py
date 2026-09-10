"""Integration with the production YunoHost server: settings files, installed apps, borg_ynh discovery."""

from __future__ import annotations

import json
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

from .logging_setup import get_logger
from .redaction import register_secret

log = get_logger("yunohost")
APPS_SETTINGS_DIR = Path("/etc/yunohost/apps")


@dataclass
class BorgYnhInstance:
    app: str  # borg, borg__2 ...
    repository: str
    passphrase: str | None
    remote_path: str | None
    install_dir: str | None
    ssh_key_path: str | None
    binary: str | None
    on_calendar: str | None = None

    def describe(self) -> str:
        return f"{self.app}: {self.repository}"


def read_app_settings(app: str, root: Path = APPS_SETTINGS_DIR) -> dict:
    path = root / app / "settings.yml"
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def read_app_manifest(app: str, root: Path = APPS_SETTINGS_DIR) -> dict:
    toml_path = root / app / "manifest.toml"
    json_path = root / app / "manifest.json"
    try:
        if toml_path.is_file():
            return tomllib.loads(toml_path.read_text(encoding="utf-8"))
        if json_path.is_file():
            return json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.debug("cannot read manifest of %s: %s", app, exc)
    return {}


def installed_apps(root: Path = APPS_SETTINGS_DIR) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "settings.yml").is_file())


def discover_borg_ynh(
    root: Path = APPS_SETTINGS_DIR, ssh_dir: Path = Path("/root/.ssh")
) -> list[BorgYnhInstance]:
    """Find installed upstream borg_ynh instances and their usable configuration."""
    instances: list[BorgYnhInstance] = []
    for app in installed_apps(root):
        manifest = read_app_manifest(app, root)
        if manifest.get("id") != "borg" and app.split("__")[0] != "borg":
            continue
        settings = read_app_settings(app, root)
        repository = str(settings.get("repository") or "")
        if not repository and settings.get("ssh_user") and settings.get("server"):
            repository = f"ssh://{settings['ssh_user']}@{settings['server']}/~/backup"
        passphrase = settings.get("passphrase")
        if passphrase:
            register_secret(str(passphrase))
        install_dir = settings.get("install_dir") or settings.get("final_path") or f"/var/www/{app}"
        binary = f"{install_dir}/venv/bin/borg"
        key = ssh_dir / f"id_{app}_ed25519"
        instances.append(
            BorgYnhInstance(
                app=app,
                repository=repository,
                passphrase=str(passphrase) if passphrase else None,
                remote_path=str(settings.get("remote_path") or "") or None,
                install_dir=str(install_dir),
                ssh_key_path=str(key) if key.is_file() else None,
                binary=binary if Path(binary).is_file() else None,
                on_calendar=settings.get("on_calendar"),
            )
        )
    return instances


def yunohost_version() -> str | None:
    try:
        out = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", "yunohost"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def known_host_entry(host: str, known_hosts: Path = Path("/root/.ssh/known_hosts")) -> str | None:
    """Return the known_hosts lines for ``host`` (``[host]:port`` form supported) if present."""
    try:
        out = subprocess.run(
            ["ssh-keygen", "-F", host, "-f", str(known_hosts)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line for line in out.stdout.splitlines() if line and not line.startswith("#")]
    return "\n".join(lines) + "\n" if lines else None

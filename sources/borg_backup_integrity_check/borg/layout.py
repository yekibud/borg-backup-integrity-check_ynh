"""Reader for the YunoHost backup layout stored inside a Borg archive.

A YunoHost backup working directory (what borg_ynh archives) looks like::

    info.json                      description, created_at, size, size_details, apps, system, from_yunohost_version
    backup.csv                     "source","dest" rows mapping live paths to archive paths
    apps/<app>/settings/           copy of /etc/yunohost/apps/<app> (manifest, settings.yml, scripts/)
    apps/<app>/backup/             files declared by the app's backup script (db.sql, /var/www/..., data dir...)
    conf/...                       system configuration parts (conf_ldap, conf_ynh_settings, ...)
    data/...                       system data parts (data_mail, data_home, data_multimedia)
    hooks/restore/...              restore hooks copied at backup time

Only the small metadata files are extracted; everything else is derived from the
archive listing.
"""

from __future__ import annotations

import csv
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..logging_setup import get_logger
from .client import BorgClient

log = get_logger("layout")

LAYOUT_PATTERNS = [
    "+ pf:info.json",
    "+ pf:backup.csv",
    "+ sh:apps/*/settings/settings.yml",
    "+ sh:apps/*/settings/manifest.toml",
    "+ sh:apps/*/settings/manifest.json",
    "- sh:**",
]


@dataclass
class BackupInfo:
    created_at: int | None = None
    description: str = ""
    size: int | None = None
    size_details: dict[str, dict[str, int]] = field(default_factory=dict)
    apps: dict[str, dict[str, Any]] = field(default_factory=dict)
    system: dict[str, dict[str, Any]] = field(default_factory=dict)
    from_yunohost_version: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackupInfo:
        system = data.get("system")
        if system is None:
            system = data.get("hooks", {})  # historical name
        return cls(
            created_at=data.get("created_at"),
            description=data.get("description", "") or "",
            size=data.get("size"),
            size_details=data.get("size_details", {}) or {},
            apps=data.get("apps", {}) or {},
            system=system or {},
            from_yunohost_version=str(data.get("from_yunohost_version", "") or ""),
        )

    @property
    def yunohost_major(self) -> int | None:
        head = self.from_yunohost_version.split(".")[0]
        return int(head) if head.isdigit() else None


@dataclass
class BackupCsvRow:
    source: str  # live absolute path on the backed-up server
    dest: str  # relative path inside the archive


@dataclass
class AppArchiveEntry:
    app: str
    settings: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def manifest_id(self) -> str:
        return str(self.manifest.get("id") or self.app.split("__")[0])

    @property
    def packaging_format(self) -> int:
        try:
            return int(self.manifest.get("packaging_format", 1))
        except (TypeError, ValueError):
            return 1

    @property
    def data_dir(self) -> str | None:
        value = self.settings.get("data_dir") or self.settings.get("datadir")
        return str(value) if value else None

    @property
    def install_dir(self) -> str | None:
        value = self.settings.get("install_dir") or self.settings.get("final_path")
        return str(value) if value else None

    @property
    def domain(self) -> str | None:
        return self.settings.get("domain")

    @property
    def path(self) -> str | None:
        return self.settings.get("path")

    @property
    def db_name(self) -> str | None:
        return self.settings.get("db_name")

    @property
    def db_type(self) -> str | None:
        resources = self.manifest.get("resources", {}) or {}
        database = resources.get("database") or {}
        if database.get("type"):
            return str(database["type"])
        if self.settings.get("psql_pwd") or self.settings.get("psqlpwd"):
            return "postgresql"
        if self.settings.get("mysqlpwd") or self.settings.get("db_pwd"):
            return "mysql"
        return None

    @property
    def manifest_data_dir(self) -> str | None:
        resources = self.manifest.get("resources", {}) or {}
        data_dir = resources.get("data_dir")
        if data_dir is None:
            return None
        directory = data_dir.get("dir") if isinstance(data_dir, dict) else None
        return str(directory or "/home/yunohost.app/__APP__").replace("__APP__", self.app)

    @property
    def services(self) -> list[str]:
        """Best-effort service names: manifest doesn't declare them, so derive from app id."""
        return [self.app]


@dataclass
class BackupLayout:
    archive: str
    info: BackupInfo
    csv_rows: list[BackupCsvRow] = field(default_factory=list)
    apps: dict[str, AppArchiveEntry] = field(default_factory=dict)

    @property
    def system_parts(self) -> list[str]:
        return sorted(self.info.system)

    def app_names(self) -> list[str]:
        return sorted(set(self.info.apps) | set(self.apps))

    def dest_for_source(self, source: str) -> str | None:
        for row in self.csv_rows:
            if row.source == source:
                return row.dest
        return None

    def source_for_dest(self, dest: str) -> str | None:
        """Map an archive path back to its live path using the longest matching CSV row."""
        best: BackupCsvRow | None = None
        for row in self.csv_rows:
            matches = dest == row.dest or dest.startswith(row.dest.rstrip("/") + "/")
            if matches and (best is None or len(row.dest) > len(best.dest)):
                best = row
        if best is None:
            return None
        remainder = dest[len(best.dest) :]
        return best.source.rstrip("/") + remainder

    def rows_for_app(self, app: str) -> list[BackupCsvRow]:
        prefix = f"apps/{app}/backup/"
        return [row for row in self.csv_rows if row.dest.startswith(prefix)]

    def rows_for_system_part(self, part: str) -> list[BackupCsvRow]:
        prefix = part.replace("_", "/", 1) + "/"
        return [row for row in self.csv_rows if row.dest.startswith(prefix)]


def parse_backup_csv(text: str) -> list[BackupCsvRow]:
    rows: list[BackupCsvRow] = []
    for record in csv.reader(text.splitlines()):
        if len(record) >= 2 and record[0]:
            rows.append(BackupCsvRow(source=record[0], dest=record[1].strip("/")))
    return rows


def load_layout_from_dir(archive: str, root: Path) -> BackupLayout:
    """Build a :class:`BackupLayout` from an extracted metadata directory."""
    info_path = root / "info.json"
    if not info_path.is_file():
        raise FileNotFoundError(f"{archive}: info.json missing (not a YunoHost backup archive?)")
    with open(info_path, encoding="utf-8") as fh:
        info = BackupInfo.from_dict(json.load(fh))
    layout = BackupLayout(archive=archive, info=info)
    csv_path = root / "backup.csv"
    if csv_path.is_file():
        layout.csv_rows = parse_backup_csv(csv_path.read_text(encoding="utf-8", errors="replace"))
    apps_dir = root / "apps"
    if apps_dir.is_dir():
        for app_dir in sorted(apps_dir.iterdir()):
            settings_dir = app_dir / "settings"
            entry = AppArchiveEntry(app=app_dir.name)
            settings_file = settings_dir / "settings.yml"
            if settings_file.is_file():
                try:
                    entry.settings = yaml.safe_load(settings_file.read_text(encoding="utf-8")) or {}
                except yaml.YAMLError as exc:
                    log.warning(
                        "%s: unreadable settings.yml for %s: %s", archive, app_dir.name, exc
                    )
            manifest_toml = settings_dir / "manifest.toml"
            manifest_json = settings_dir / "manifest.json"
            try:
                if manifest_toml.is_file():
                    entry.manifest = tomllib.loads(manifest_toml.read_text(encoding="utf-8"))
                elif manifest_json.is_file():
                    entry.manifest = json.loads(manifest_json.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
                log.warning("%s: unreadable manifest for %s: %s", archive, app_dir.name, exc)
            layout.apps[app_dir.name] = entry
    return layout


def read_layout(client: BorgClient, archive: str, workdir: Path) -> BackupLayout:
    """Extract only the metadata files of an archive and parse them."""
    target = workdir / "layout" / archive.replace("/", "_")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    client.extract(archive, target, patterns=LAYOUT_PATTERNS)
    return load_layout_from_dir(archive, target)

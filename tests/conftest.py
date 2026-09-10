"""Shared fixtures: synthetic archive listings and YunoHost backup layouts (no Borg needed)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from borg_backup_integrity_check.borg.layout import (
    AppArchiveEntry,
    BackupCsvRow,
    BackupInfo,
    BackupLayout,
)
from borg_backup_integrity_check.borg.models import ArchiveItem, ArchiveRef

NOW = datetime(2026, 9, 10, 9, 0, 0)


def make_item(
    path: str,
    size: int = 1024,
    mtime: datetime | None = None,
    type_: str = "-",
    user: str = "app",
    healthy: bool = True,
) -> ArchiveItem:
    return ArchiveItem(
        path=path,
        type=type_,
        size=size if type_ == "-" else 0,
        mtime=mtime or NOW,
        mode="-rw-r--r--",
        user=user,
        group=user,
        uid=1000,
        gid=1000,
        healthy=healthy,
    )


def dir_items(path: str) -> list[ArchiveItem]:
    """Directory entries for every prefix of ``path`` (Borg lists directories explicitly)."""
    items = []
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        items.append(make_item("/".join(parts[:i]), type_="d"))
    return items


@pytest.fixture
def synthetic_app_listing() -> list[ArchiveItem]:
    """A 'filebox' app archive: settings, db dump, install dir and a data dir with many user files."""
    items: list[ArchiveItem] = []
    seen: set[str] = set()

    def add_dirs(p: str) -> None:
        for d in dir_items(p):
            if d.path not in seen:
                seen.add(d.path)
                items.append(d)

    add_dirs("apps/filebox/settings/scripts")
    items += [
        make_item("apps/filebox/settings/settings.yml", 400),
        make_item("apps/filebox/settings/manifest.toml", 900),
        make_item("apps/filebox/settings/scripts/restore", 2000),
        make_item("apps/filebox/backup/db.sql", 5 * 1024 * 1024),
    ]
    add_dirs("apps/filebox/backup/var/www/filebox/vendor")
    for i in range(30):
        items.append(
            make_item(
                f"apps/filebox/backup/var/www/filebox/vendor/lib{i}.php",
                20_000,
                NOW - timedelta(days=400),
            )
        )
    add_dirs("apps/filebox/backup/home/yunohost.app/filebox/alice/files/Photos")
    add_dirs("apps/filebox/backup/home/yunohost.app/filebox/alice/files/Documents")
    add_dirs("apps/filebox/backup/home/yunohost.app/filebox/alice/cache")
    add_dirs("apps/filebox/backup/home/yunohost.app/filebox/bob/files")
    add_dirs("apps/filebox/backup/home/yunohost.app/filebox/appdata_x1/preview")
    for i in range(120):
        items.append(
            make_item(
                f"apps/filebox/backup/home/yunohost.app/filebox/alice/files/Photos/IMG_{i:04d}.jpg",
                3_000_000,
                NOW - timedelta(hours=i),
            )
        )
    for i in range(40):
        items.append(
            make_item(
                f"apps/filebox/backup/home/yunohost.app/filebox/alice/files/Documents/doc{i}.odt",
                50_000,
                NOW - timedelta(days=i),
            )
        )
    for i in range(50):
        items.append(
            make_item(
                f"apps/filebox/backup/home/yunohost.app/filebox/bob/files/report{i}.pdf",
                200_000,
                NOW - timedelta(minutes=30 * i),
            )
        )
    for i in range(200):
        items.append(
            make_item(
                f"apps/filebox/backup/home/yunohost.app/filebox/appdata_x1/preview/{i}.png",
                10_000,
                NOW,
            )
        )
    for i in range(10):
        items.append(
            make_item(
                f"apps/filebox/backup/home/yunohost.app/filebox/alice/cache/tmp{i}", 1000, NOW
            )
        )
    items.append(
        make_item("apps/filebox/backup/home/yunohost.app/filebox/alice/files/.hidden", 10, NOW)
    )
    items.append(
        make_item("apps/filebox/backup/home/yunohost.app/filebox/alice/files/empty.txt", 0, NOW)
    )
    return items


@pytest.fixture
def synthetic_app_layout() -> BackupLayout:
    info = BackupInfo.from_dict(
        {
            "created_at": int(NOW.timestamp()),
            "size": 400_000_000,
            "size_details": {"system": {}, "apps": {"filebox": 400_000_000}},
            "apps": {"filebox": {"version": "1.0~ynh1", "name": "Filebox", "description": "test"}},
            "system": {},
            "from_yunohost_version": "12.1.41",
        }
    )
    layout = BackupLayout(archive="auto_filebox-2026-09-10T02:00:00", info=info)
    layout.csv_rows = [
        BackupCsvRow("/var/www/filebox", "apps/filebox/backup/var/www/filebox"),
        BackupCsvRow("/home/yunohost.app/filebox", "apps/filebox/backup/home/yunohost.app/filebox"),
        BackupCsvRow("/etc/yunohost/apps/filebox", "apps/filebox/settings"),
    ]
    layout.apps["filebox"] = AppArchiveEntry(
        app="filebox",
        settings={
            "id": "filebox",
            "domain": "example.org",
            "path": "/files",
            "data_dir": "/home/yunohost.app/filebox",
            "install_dir": "/var/www/filebox",
            "db_name": "filebox",
            "db_pwd": "secret",
        },
        manifest={
            "id": "filebox",
            "packaging_format": 2,
            "resources": {"data_dir": {}, "database": {"type": "mysql"}},
        },
    )
    return layout


@pytest.fixture
def archive_ref() -> ArchiveRef:
    return ArchiveRef(
        name="auto_filebox-2026-09-10T02:00:00", id="abc", start=datetime(2026, 9, 10, 2, 0, 0)
    )


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def write_json_lines(path: Path, items: list[ArchiveItem]) -> None:
    import gzip

    with gzip.open(path, "wt") as fh:
        for it in items:
            fh.write(
                json.dumps(
                    {
                        "path": it.path,
                        "type": it.type,
                        "size": it.size,
                        "mtime": it.mtime.isoformat(),
                        "user": it.user,
                        "group": it.group,
                        "healthy": it.healthy,
                    }
                )
                + "\n"
            )

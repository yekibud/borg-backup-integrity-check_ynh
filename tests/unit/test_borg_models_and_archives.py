import json
import re
from datetime import datetime, timedelta

import pytest

from borg_backup_integrity_check.borg.archives import (
    ArchiveCatalog,
    ArchiveNamingScheme,
    component_app_id,
    component_kind,
)
from borg_backup_integrity_check.borg.client import exit_code_category
from borg_backup_integrity_check.borg.models import (
    ArchiveItem,
    ArchiveRef,
    ArchiveStats,
    RepositoryInfo,
)

LIST_JSON = {
    "archives": [
        {
            "archive": "auto_conf-2026-09-09T02:00:00",
            "barchive": "x",
            "id": "1" * 64,
            "name": "auto_conf-2026-09-09T02:00:00",
            "start": "2026-09-09T02:00:00.000000",
            "time": "2026-09-09T02:00:00.000000",
        },
        {
            "archive": "auto_nextcloud-2026-09-09T02:10:00",
            "id": "2" * 64,
            "name": "auto_nextcloud-2026-09-09T02:10:00",
            "start": "2026-09-09T02:10:00.000000",
        },
        {
            "archive": "auto_conf-2026-09-10T02:00:00",
            "id": "3" * 64,
            "name": "auto_conf-2026-09-10T02:00:00",
            "start": "2026-09-10T02:00:00.000000",
        },
        {
            "archive": "auto_nextcloud-2026-09-10T02:11:00",
            "id": "4" * 64,
            "name": "auto_nextcloud-2026-09-10T02:11:00",
            "start": "2026-09-10T02:11:00.000000",
        },
        {
            "archive": "auto_data-2026-09-08T02:20:00",
            "id": "5" * 64,
            "name": "auto_data-2026-09-08T02:20:00",
            "start": "2026-09-08T02:20:00.000000",
        },
        {
            "archive": "manual-before-upgrade",
            "id": "6" * 64,
            "name": "manual-before-upgrade",
            "start": "2026-09-01T10:00:00.000000",
        },
    ]
}


def test_archive_ref_and_stats_parsing():
    refs = [ArchiveRef.from_json(a) for a in LIST_JSON["archives"]]
    assert refs[0].name == "auto_conf-2026-09-09T02:00:00"
    assert refs[0].start == datetime(2026, 9, 9, 2, 0)
    stats = ArchiveStats.from_json(
        {
            "stats": {
                "original_size": 10,
                "compressed_size": 5,
                "deduplicated_size": 2,
                "nfiles": 3,
            },
            "hostname": "h",
            "command_line": ["borg", "create"],
        }
    )
    assert (stats.original_size, stats.compressed_size, stats.deduplicated_size, stats.nfiles) == (
        10,
        5,
        2,
        3,
    )
    repo = RepositoryInfo.from_json(
        {
            "repository": {
                "id": "ab",
                "location": "ssh://x/repo",
                "last_modified": "2026-09-10T02:11:00.000000",
            },
            "encryption": {"mode": "repokey"},
        }
    )
    assert repo.encryption_mode == "repokey" and repo.last_modified.year == 2026


def test_archive_item_from_json_lines():
    line = json.loads(
        '{"type": "-", "mode": "-rw-r--r--", "user": "vmail", "group": "mail", "uid": 5000, "gid": 8, "path": "data/mail/alice/cur/1.eml", "healthy": true, "source": "", "linktarget": "", "flags": null, "mtime": "2026-09-10T08:31:00.000000", "size": 1234}'
    )
    item = ArchiveItem.from_json(line)
    assert (
        item.is_file
        and item.size == 1234
        and item.name == "1.eml"
        and item.parent == "data/mail/alice/cur"
    )
    assert item.mtime == datetime(2026, 9, 10, 8, 31)
    d = ArchiveItem.from_json({"type": "d", "path": "data", "mtime": None})
    assert d.is_dir and d.parent == ""


def test_naming_scheme_and_kinds():
    scheme = ArchiveNamingScheme()
    assert scheme.parse("auto_nextcloud-2026-09-10T02:11:00") == (
        "auto_nextcloud",
        "2026-09-10T02:11:00",
    )
    assert scheme.parse("manual-before-upgrade") is None
    assert ArchiveNamingScheme.from_settings("single").parse("anything") == ("all", None)
    assert component_kind("auto_conf") == "system_conf"
    assert component_kind("auto_data") == "system_data"
    assert component_kind("auto_nextcloud") == "app"
    assert component_app_id("auto_nextcloud") == "nextcloud"
    assert component_app_id("auto_conf") is None
    with pytest.raises(re.error):
        ArchiveNamingScheme.from_settings("custom", "(unclosed")


def test_generation_selection_groups_recent_archives_and_flags_stale():
    refs = [ArchiveRef.from_json(a) for a in LIST_JSON["archives"]]
    catalog = ArchiveCatalog.build(ArchiveNamingScheme(), refs)
    assert [r.name for r in catalog.unparsed] == ["manual-before-upgrade"]
    latest = catalog.latest_generation(window=timedelta(hours=12))
    assert latest is not None
    assert latest.timestamp == datetime(2026, 9, 10, 2, 11)
    assert set(latest.archives) == {"auto_conf", "auto_nextcloud"}
    assert "auto_data" in latest.stale  # newest auto_data is two days old
    previous = catalog.previous_generation(latest)
    assert previous is not None and set(previous.archives) == {"auto_conf", "auto_nextcloud"}
    assert previous.timestamp == datetime(2026, 9, 9, 2, 10)
    older = catalog.previous_generation(previous)
    assert older is not None and set(older.archives) == {"auto_data"}
    assert latest.id == "20260910T021100"


def test_exit_code_categories():
    assert exit_code_category(0) == "ok"
    assert exit_code_category(1) == "warning"
    assert exit_code_category(2) == "error"
    assert exit_code_category(73) == "lock"
    assert exit_code_category(82) == "connection"
    assert exit_code_category(52) == "passphrase"
    assert exit_code_category(105) == "warning"

"""The restore spec the engine sends to the host for a sparsely restored component."""

from tests.conftest import dir_items, make_item

from borg_backup_integrity_check.borg.listing import DirectoryAggregates
from borg_backup_integrity_check.discovery.components import Component, LargeRoot
from borg_backup_integrity_check.restore.engine import CoreRestoreEngine
from borg_backup_integrity_check.restore.host_helper import core_patterns

ROOT = "apps/immich/backup/home/yunohost.app/immich"


def test_skeleton_carries_directories_and_the_kept_plumbing_files(archive_ref):
    items = dir_items(f"{ROOT}/backups") + dir_items(f"{ROOT}/upload/thumbs")
    items.append(make_item(f"{ROOT}/backups/restore_immich_db_backup.sh", 1200))
    comp = Component(
        id="immich",
        kind="app",
        archive=archive_ref,
        archive_component="auto_immich",
        root="apps/immich",
        layout=None,
    )
    comp.large_roots = [
        LargeRoot(
            archive_path=ROOT,
            live_path="/home/yunohost.app/immich",
            origin="profile",
            keep_dirs=[f"{ROOT}/backups"],
            keep_files=[f"{ROOT}/.env"],
            keep_bytes=1500,
        )
    ]
    engine = CoreRestoreEngine(
        agent=None, aggregates={archive_ref.name: DirectoryAggregates.build(items)}, ssh_port=22022
    )
    skeleton = engine._skeleton(comp, comp.large_roots[0])["skeleton"]

    directories = [e["path"] for e in skeleton if "kind" not in e]
    assert ROOT in directories and f"{ROOT}/upload" in directories
    assert [e["path"] for e in skeleton if e.get("kind") == "subtree"] == [f"{ROOT}/backups"]
    assert [e["path"] for e in skeleton if e.get("kind") == "file"] == [f"{ROOT}/.env"]
    # And the host turns that into borg patterns that survive the root's exclusion.
    patterns = core_patterns([engine._skeleton(comp, comp.large_roots[0])])
    assert patterns[-1] == f"- pp:{ROOT}"
    assert f"+ pp:{ROOT}/backups" in patterns[:-1]  # prefix: the whole directory
    assert f"+ pf:{ROOT}/.env" in patterns[:-1]  # full path: exactly that file
    assert f"+ pf:{ROOT}" in patterns[:-1]

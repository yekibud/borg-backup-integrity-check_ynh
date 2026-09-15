"""Tests for pure parts of the on-host helper (no borg/yunohost needed)."""

import json
import subprocess
import sys
from pathlib import Path

from borg_backup_integrity_check.restore import host_helper


def test_apply_owner_parses_mode(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    host_helper._apply_owner(d, {"mode": "drwxr-x---", "user": None, "group": None})
    assert oct(d.stat().st_mode & 0o777) == "0o750"


def test_describe_command_runs_out_of_process(tmp_path):
    (tmp_path / "IMG_1.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9")
    (tmp_path / "mail").write_bytes(
        b"Subject: Hello\nDate: Thu, 10 Sep 2026 08:31:00 +0000\n\nbody\n"
    )
    spec = {
        "objects": [
            {
                "archive_path": "a/IMG_1.jpg",
                "live_path": str(tmp_path / "IMG_1.jpg"),
                "kind": "file",
                "relative_path": "IMG_1.jpg",
            },
            {
                "archive_path": "a/mail",
                "live_path": str(tmp_path / "mail"),
                "kind": "file",
                "relative_path": "alice/cur/mail",
            },
            {
                "archive_path": "a/missing",
                "live_path": str(tmp_path / "missing"),
                "kind": "file",
                "relative_path": "missing",
            },
        ]
    }
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "sources")}
    proc = subprocess.run(
        [sys.executable, "-m", "borg_backup_integrity_check.restore.host_helper", "describe"],
        input=json.dumps(spec).encode(),
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.decode().splitlines()[-1])
    by = {o["archive_path"]: o["evidence"] for o in data["objects"]}
    assert (
        by["a/IMG_1.jpg"]["kind"] == "image"
        and by["a/IMG_1.jpg"]["details"]["relative_path"] == "IMG_1.jpg"
    )
    assert (
        by["a/mail"]["kind"] == "email"
        and by["a/mail"]["title"] == "Hello"
        and "body" not in json.dumps(by["a/mail"])
    )
    assert by["a/missing"]["readable"] is False


def test_unknown_command_reports_json_error():
    proc = subprocess.run(
        [sys.executable, "-m", "borg_backup_integrity_check.restore.host_helper", "nope"],
        capture_output=True,
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "sources")},
        check=False,
    )
    assert proc.returncode != 0


def test_core_and_payload_patterns():
    core = host_helper.core_patterns(
        [
            {
                "archive_path": "apps/x/backup/home/yunohost.app/x",
                "skeleton": [
                    {"path": "apps/x/backup/home/yunohost.app/x"},
                    {"path": "apps/x/backup/home/yunohost.app/x/alice"},
                ],
            }
        ]
    )
    assert core == [
        "+ pf:apps/x/backup/home/yunohost.app/x",
        "+ pf:apps/x/backup/home/yunohost.app/x/alice",
        "- pp:apps/x/backup/home/yunohost.app/x",
    ]
    root = {
        "archive_path": "apps/x/backup/home/yunohost.app/x",
        "live_path": "/home/yunohost.app/x",
        "objects": [
            {
                "archive_path": "apps/x/backup/home/yunohost.app/x/alice/files/Photos/IMG_1.jpg",
                "kind": "file",
            },
            {
                "archive_path": "apps/x/backup/home/yunohost.app/x/alice/files/Photos/IMG_2.jpg",
                "kind": "file",
            },
            {
                "archive_path": "apps/x/backup/home/yunohost.app/x/alice/projects/tool",
                "kind": "git_repo",
            },
        ],
    }
    pats = host_helper.payload_patterns(root)
    assert pats[0] == "+ pf:apps/x/backup/home/yunohost.app/x/alice/files/Photos/IMG_1.jpg"
    assert (
        "+ pf:apps/x/backup/home/yunohost.app/x/alice/files/Photos" in pats
        and "+ pf:apps/x/backup/home/yunohost.app/x" in pats
    )
    assert pats.count("+ pf:apps/x/backup/home/yunohost.app/x/alice") == 1  # ancestors added once
    assert "+ pf:apps/x/backup/home/yunohost.app/x/alice/projects/tool/.git/HEAD" in pats
    assert "+ sh:apps/x/backup/home/yunohost.app/x/alice/projects/tool/.git/refs/**" in pats
    assert pats[-1] == "- sh:**"
    assert not any(
        p.startswith("+ pf:apps/x/backup/home/yunohost.app/x/alice/projects/tool/.git/objects")
        for p in pats
    )
    assert host_helper.payload_patterns(
        {"archive_path": "data/mail", "live_path": "/var/mail", "full": True}
    ) == ["+ pp:data/mail", "- sh:**"]


def test_operation_log_text_prefers_the_log_beside_the_metadata(tmp_path):
    (tmp_path / "op.yml").write_text("metadata: only\n")
    (tmp_path / "op.log").write_text("ERROR the app restore script exited 1\n")
    text = host_helper._operation_log_text(str(tmp_path / "op.yml"))
    assert text == "ERROR the app restore script exited 1\n"


def test_operation_log_text_falls_back_to_the_metadata_file(tmp_path):
    (tmp_path / "op.yml").write_text("metadata: only\n")
    assert host_helper._operation_log_text(str(tmp_path / "op.yml")) == "metadata: only\n"
    assert host_helper._operation_log_text(None) is None
    assert host_helper._operation_log_text(str(tmp_path / "gone.yml")) is None


def test_operation_log_text_keeps_the_end_of_a_long_log(tmp_path):
    (tmp_path / "op.log").write_text("x" * 500 + "the actual error\n")
    text = host_helper._operation_log_text(str(tmp_path / "op.log"), limit=100)
    assert text.startswith("[...truncated...]\n") and text.endswith("the actual error\n")
    assert len(text) < 200


def test_core_patterns_keep_plumbing_files_out_of_the_exclusion():
    """Order matters: borg takes the first matching pattern, so the includes precede the exclusion."""
    root = "apps/immich/backup/home/yunohost.app/immich"
    patterns = host_helper.core_patterns(
        [
            {
                "archive_path": root,
                "skeleton": [
                    {"path": root},
                    {"path": f"{root}/upload"},
                    {"path": f"{root}/backups", "kind": "subtree"},
                    {"path": f"{root}/.env", "kind": "file"},
                ],
            }
        ]
    )
    assert patterns == [
        f"+ pf:{root}",
        f"+ pf:{root}/upload",
        f"+ pp:{root}/backups",
        f"+ pf:{root}/.env",
        f"- pp:{root}",
    ]


def test_skeleton_file_entries_are_not_recreated_as_directories(tmp_path):
    """A kept file is extracted by borg; mkdir-ing its path would shadow it with a directory."""
    work = tmp_path / "work"
    (work / "root/backups").mkdir(parents=True)
    kept = work / "root/backups/restore.sh"
    kept.write_text("#!/bin/bash\n")
    host_helper.materialise_skeleton(
        work,
        [
            {
                "archive_path": "root",
                "skeleton": [
                    {"path": "root"},
                    {"path": "root/backups"},
                    {"path": "root/backups/restore.sh", "kind": "file"},
                    {"path": "root/conf", "kind": "subtree"},
                    {"path": "root/upload"},
                ],
            }
        ],
    )
    assert kept.is_file() and kept.read_text() == "#!/bin/bash\n"
    assert not (work / "root/conf").exists()  # borg extracted the subtree, or it is not there
    assert (work / "root/upload").is_dir()  # plain directory entries are still recreated


def _apt_dirs(tmp_path):
    """The four directories plus sources.list itself, in the order the helper reads them."""
    dirs = tuple(
        tmp_path / d for d in ("sources.list.d", "preferences.d", "keyrings", "trusted.gpg.d")
    )
    for d in dirs:
        d.mkdir()
    return dirs + (tmp_path / "sources.list",)


def test_a_failed_restore_stops_poisoning_the_next_app(tmp_path):
    """immich's failed restore left jellyfin.list behind; jitsi then died on that repo's mirror."""
    dirs = _apt_dirs(tmp_path)
    (dirs[0] / "yunohost.list").write_text(
        "deb https://forge.yunohost.org/debian bookworm stable\n"
    )
    before = host_helper.apt_source_snapshot(dirs)

    (dirs[0] / "jellyfin.list").write_text("deb https://repo.jellyfin.org/debian bookworm main\n")
    (dirs[1] / "jellyfin.pref").write_text("Package: *\nPin: origin repo.jellyfin.org\n")
    (dirs[2] / "jellyfin.gpg").write_bytes(b"\x99\x01binary key")
    (dirs[0] / "yunohost.list").write_text(
        "deb https://forge.yunohost.org/debian bookworm edited\n"
    )

    changed = host_helper.revert_apt_sources(before, dirs)

    assert not (dirs[0] / "jellyfin.list").exists()
    assert not (dirs[1] / "jellyfin.pref").exists()
    assert not (dirs[2] / "jellyfin.gpg").exists()
    assert "bookworm stable" in (dirs[0] / "yunohost.list").read_text()  # edit undone
    assert len(changed) == 4


def test_a_source_file_the_restore_deleted_comes_back(tmp_path):
    dirs = _apt_dirs(tmp_path)
    (dirs[0] / "sury.list").write_text("deb https://packages.sury.org/php bookworm main\n")
    before = host_helper.apt_source_snapshot(dirs)
    (dirs[0] / "sury.list").unlink()

    assert host_helper.revert_apt_sources(before, dirs) == [f"restored {dirs[0] / 'sury.list'}"]
    assert (dirs[0] / "sury.list").read_text().startswith("deb https://packages.sury.org")


def test_an_edit_to_sources_list_itself_is_undone(tmp_path):
    dirs = _apt_dirs(tmp_path)
    sources_list = dirs[-1]
    sources_list.write_text("deb http://deb.debian.org/debian bookworm main\n")
    before = host_helper.apt_source_snapshot(dirs)
    sources_list.write_text("deb http://deb.debian.org/debian bookworm main\ndeb http://x/ y z\n")

    assert host_helper.revert_apt_sources(before, dirs) == [f"reverted {sources_list}"]
    assert sources_list.read_text() == "deb http://deb.debian.org/debian bookworm main\n"


def test_nothing_is_touched_when_the_restore_added_nothing(tmp_path):
    dirs = _apt_dirs(tmp_path)
    (dirs[0] / "yunohost.sources").write_text("Types: deb\nURIs: https://forge.yunohost.org\n")
    before = host_helper.apt_source_snapshot(dirs)

    assert host_helper.revert_apt_sources(before, dirs) == []
    assert (dirs[0] / "yunohost.sources").exists()  # deb822 files are covered like any other


def test_mount_large_roots_overlays_each_root(tmp_path, monkeypatch):
    calls = []

    def fake_sh(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:1] == ["mount"] or cmd[:1] == ["modprobe"]:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    mount_root = tmp_path / "mnt"
    archive_dir = mount_root / "archives" / "auto_immich-2026-09-15T00_06_15"
    lower = archive_dir / "apps/immich/backup/home/yunohost.app/immich"
    lower.mkdir(parents=True)
    live = tmp_path / "live" / "immich"

    monkeypatch.setattr(host_helper, "sh", fake_sh)
    monkeypatch.setattr(host_helper, "MOUNT_ROOT", mount_root)
    monkeypatch.setattr(host_helper, "borg_env", lambda: {"BBIC_LOCK_WAIT": "900"})
    monkeypatch.setattr(host_helper, "_borg_candidates", lambda: ["/usr/bin/borg"])
    monkeypatch.setattr(host_helper.os.path, "ismount", lambda path: True)
    monkeypatch.setattr(host_helper, "_enable_metacopy", lambda: True)
    spec = {
        "archive": "auto_immich-2026-09-15T00:06:15",
        "roots": [
            {
                "archive_path": "apps/immich/backup/home/yunohost.app/immich",
                "live_path": str(live),
            }
        ],
    }
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(spec)))
    result = host_helper.cmd_mount_large_roots(None)

    assert result["ok"] and result["mounted"] == [str(live)] and result["metacopy"] is True
    overlay = next(c for c in calls if c[:3] == ["mount", "-t", "overlay"])
    options = overlay[overlay.index("-o") + 1]
    assert "metacopy=on" in options and f"lowerdir={lower}" in options
    assert live.is_dir()


def test_mount_large_roots_reports_a_root_missing_from_the_archive(tmp_path, monkeypatch):
    mount_root = tmp_path / "mnt"
    (mount_root / "archives" / "auto_x").mkdir(parents=True)
    monkeypatch.setattr(
        host_helper, "sh", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, b"", b"")
    )
    monkeypatch.setattr(host_helper, "MOUNT_ROOT", mount_root)
    monkeypatch.setattr(host_helper, "borg_env", lambda: {})
    monkeypatch.setattr(host_helper, "_borg_candidates", lambda: ["/usr/bin/borg"])
    monkeypatch.setattr(host_helper.os.path, "ismount", lambda path: True)
    monkeypatch.setattr(host_helper, "_enable_metacopy", lambda: False)
    spec = {
        "archive": "auto_x",
        "roots": [{"archive_path": "nope", "live_path": str(tmp_path / "l")}],
    }
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(spec)))
    result = host_helper.cmd_mount_large_roots(None)

    assert result["ok"] is False and "not a directory" in result["errors"][0]


def test_unmount_large_roots_releases_overlays_and_archive_mounts(tmp_path, monkeypatch):
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/sda1 / ext4 rw 0 0\n"
        "bbic-overlay /home/yunohost.app/immich overlay rw 0 0\n"
        f"borgfs {tmp_path}/mnt/archives/auto_immich fuse.borgfs ro 0 0\n"
        "tmpfs /run tmpfs rw 0 0\n"
    )
    unmounted = []

    def fake_sh(cmd, **kwargs):
        unmounted.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(host_helper, "sh", fake_sh)
    monkeypatch.setattr(host_helper, "MOUNTS_FILE", mounts)
    monkeypatch.setattr(host_helper, "MOUNT_ROOT", tmp_path / "mnt")
    result = host_helper.cmd_unmount_large_roots(None)

    assert result["ok"]
    assert "/home/yunohost.app/immich" in result["unmounted"]
    assert any("auto_immich" in target for target in result["unmounted"])
    assert all(cmd[0] == "umount" for cmd in unmounted)

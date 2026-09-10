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

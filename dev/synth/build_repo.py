#!/usr/bin/env python3
"""Build a synthetic Borg repository that looks like borg_ynh backups of a small YunoHost server.

Scenarios (``--scenario``): normal (3 generations, slight growth), shrink (last generation loses most
files), grow (last generation doubles the photo set), missing (last generation lacks the app archive),
emptydb (last db.sql is empty). The layout mirrors real YunoHost backups: info.json, backup.csv,
apps/<app>/{settings,backup}, conf/, data/mail. Content is deterministic and recognisable
(e-mail subjects, JPEGs with EXIF dates, MP4s with duration, ODT documents, a git repository).

usage: build_repo.py REPO_DIR [--scenario normal] [--generations 3] [--app filebox] [--passphrase synthetic]
Requires a `borg` binary (BORG env var or PATH).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

APP_MANIFEST = """packaging_format = 2
id = "{app}"
name = "Filebox"
version = "1.0~ynh1"
[resources.system_user]
[resources.install_dir]
[resources.data_dir]
[resources.database]
type = "mysql"
"""

PHOTO_BYTES = int(os.environ.get("SYNTH_PHOTO_BYTES", 1024 * 1024))
MAIL_COUNT = int(os.environ.get("SYNTH_MAILS", 260))

RESTORE_SCRIPT = """#!/bin/bash
source /usr/share/yunohost/helpers
ynh_script_progression "Restoring $app..."
ynh_restore "$install_dir"
ynh_restore "$data_dir"
ynh_mysql_db_shell < ./db.sql
ynh_script_progression "Restoration completed for $app"
"""


def jpeg(width: int, height: int, when: datetime) -> bytes:
    dt = when.strftime("%Y:%m:%d %H:%M:%S").encode() + b"\x00"
    tiff = bytearray(b"II*\x00" + struct.pack("<I", 8))
    n = 2
    data_area = 8 + 2 + n * 12 + 4
    ifd = struct.pack("<H", n)
    ifd += struct.pack("<HHII", 0x0132, 2, len(dt), data_area)
    exif_off = data_area + len(dt)
    ifd += struct.pack("<HHII", 0x8769, 4, 1, exif_off)
    ifd += struct.pack("<I", 0)
    tiff += ifd + dt
    exif = (
        struct.pack("<H", 1)
        + struct.pack("<HHII", 0x9003, 2, len(dt), exif_off + 2 + 12 + 4)
        + struct.pack("<I", 0)
        + dt
    )
    tiff += exif
    app1 = b"Exif\x00\x00" + bytes(tiff)
    sof0 = struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    filler = os.urandom(PHOTO_BYTES)  # realistic size, non-deduplicable
    return (
        b"\xff\xd8"
        + b"\xff\xe1"
        + struct.pack(">H", len(app1) + 2)
        + app1
        + b"\xff\xc0"
        + sof0
        + b"\xff\xd9"
        + filler
    )


def mp4(duration: float) -> bytes:
    mvhd_body = (
        b"\x00\x00\x00\x00"
        + b"\x00" * 8
        + struct.pack(">I", 1000)
        + struct.pack(">I", int(duration * 1000))
        + b"\x00" * 80
    )
    mvhd = struct.pack(">I4s", 8 + len(mvhd_body), b"mvhd") + mvhd_body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp_body = b"isom\x00\x00\x02\x00isomiso2mp41"
    ftyp = struct.pack(">I4s", 8 + len(ftyp_body), b"ftyp") + ftyp_body
    mdat = os.urandom(4096)
    return ftyp + moov + struct.pack(">I4s", 8 + len(mdat), b"mdat") + mdat


def odt(path: Path, title: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr(
            "content.xml",
            f"<office:document-content><text:p>{title}</text:p></office:document-content>",
        )
        zf.writestr("filler.bin", os.urandom(1024))


def email(subject: str, when: datetime, i: int) -> bytes:
    return (
        f"Return-Path: <alice@example.org>\nFrom: Alice Example <alice@example.org>\nTo: bob@example.org\n"
        f"Subject: {subject}\nDate: {when.strftime('%a, %d %b %Y %H:%M:%S +0200')}\nMessage-ID: <synthetic-{i}@example.org>\n"
        f"MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\nThis is the body of message {i}. It must never appear in reports.\n"
    ).encode()


def git_repo(path: Path, when: datetime) -> None:
    g = path / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "logs").mkdir()
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    sha = "%040x" % (int(when.timestamp()) * 7919)
    (g / "refs" / "heads" / "main").write_text(sha + "\n")
    (g / "logs" / "HEAD").write_text(
        f"{'0' * 40} {sha} Alice <alice@example.org> {int(when.timestamp())} +0200\tcommit: Add synthetic feature\n"
    )
    (path / "README.md").write_text("# tool\n")
    (path / "src").mkdir()
    (path / "src" / "main.py").write_text("print('hello')\n")


def set_mtime(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def build_workdir(
    root: Path, app: str, when: datetime, scenario: str, gen: int, total: int, domain: str
) -> dict[str, Path]:
    """Create one 'backup run' (conf, data, app) work dirs like YunoHost does."""
    last = gen == total - 1
    photos = 60 if not (scenario == "grow" and last) else 120
    docs = 15
    mails = MAIL_COUNT
    if scenario == "shrink" and last:
        photos, mails = 5, 4
    dirs: dict[str, Path] = {}

    # ---- system conf archive
    conf = root / f"auto_conf-{gen}"
    (conf / "conf" / "ldap").mkdir(parents=True)
    (conf / "conf" / "ynh").mkdir(parents=True)
    (conf / "conf" / "ldap" / "dc=yunohost-dc=org.ldif").write_text(
        "dn: dc=yunohost,dc=org\nobjectClass: top\n" * 200
    )
    (conf / "conf" / "ldap" / "cn=config.master.ldif").write_text("dn: cn=config\n")
    (conf / "conf" / "ldap" / "ldap.conf").write_text("BASE dc=yunohost,dc=org\n")
    (conf / "conf" / "ynh" / "current_host").write_text(domain + "\n")
    (conf / "conf" / "ynh" / "settings.yml").write_text("security.ssh.port: 22\n")
    (conf / "conf" / "ynh" / "firewall.yml").write_text("tcp:\n  open: [22, 80, 443]\n")
    (conf / "conf" / "ynh" / "permissions.yml").write_text("{}\n")
    csv = [
        ("/etc/ldap/ldap.conf", "conf/ldap/ldap.conf"),
        ("/etc/yunohost/current_host", "conf/ynh/current_host"),
    ]
    _write_meta(
        conf,
        when,
        csv,
        apps={},
        system={
            "conf_ldap": {"paths": ["conf/ldap"]},
            "conf_ynh_settings": {"paths": ["conf/ynh"]},
        },
    )
    dirs["auto_conf"] = conf

    # ---- system data archive (mail)
    data = root / f"auto_data-{gen}"
    for user, count in (("alice", mails), ("bob", max(mails // 4, 1))):
        for sub in ("cur", "new", "tmp"):
            (data / "data" / "mail" / user / sub).mkdir(parents=True)
        (data / "data" / "mail" / user / "dovecot.index.log").write_bytes(b"\x00" * 128)
        for i in range(count):
            msg_when = when - timedelta(hours=i * 3 + (0 if user == "alice" else 1))
            subject = [
                "Re: Tuesday meeting",
                "Your invoice is ready",
                "Weekend plans",
                "Photos from the trip",
                "Server maintenance window",
            ][i % 5] + f" #{i}"
            p = (
                data
                / "data"
                / "mail"
                / user
                / ("new" if i % 7 == 0 else "cur")
                / f"{int(msg_when.timestamp())}.M{i}P1.host,S=812:2,S"
            )
            p.write_bytes(email(subject, msg_when, i))
            set_mtime(p, msg_when)
        (data / "data" / "mail" / user / "tmp" / "inflight.eml").write_bytes(b"partial")
    _write_meta(
        data,
        when,
        [("/var/mail", "data/mail")],
        apps={},
        system={"data_mail": {"paths": ["data/mail"]}},
    )
    dirs["auto_data"] = data

    # ---- app archive
    appdir = root / f"auto_{app}-{gen}"
    settings = appdir / "apps" / app / "settings"
    (settings / "scripts").mkdir(parents=True)
    (settings / "settings.yml").write_text(
        f"id: {app}\ndomain: {domain}\npath: /files\ninstall_dir: /var/www/{app}\ndata_dir: /home/yunohost.app/{app}\ndb_name: {app}\ndb_user: {app}\ndb_pwd: synthetic-db-password\n"
    )
    (settings / "manifest.toml").write_text(APP_MANIFEST.format(app=app))
    (settings / "scripts" / "restore").write_text(RESTORE_SCRIPT)
    backup = appdir / "apps" / app / "backup"
    www = backup / "var" / "www" / app
    (www / "vendor").mkdir(parents=True)
    (www / "index.php").write_text("<?php echo 'filebox';\n")
    for i in range(20):
        (www / "vendor" / f"lib{i}.php").write_text("<?php // vendor lib\n" * 50)
    db = backup / "db.sql"
    if scenario == "emptydb" and last:
        db.write_bytes(b"")
    else:
        rows = "\n".join(
            f"INSERT INTO files VALUES ({i}, 'IMG_{i:04d}.jpg', 'alice');" for i in range(photos)
        )
        db.write_text(
            "CREATE TABLE files (id int, name varchar(255), owner varchar(64));\n"
            + rows
            + "\n"
            + "-- filler\n" * 200000
        )
    datad = backup / "home" / "yunohost.app" / app
    for user in ("alice", "bob"):
        (datad / user / "files" / "Photos").mkdir(parents=True)
        (datad / user / "files" / "Documents").mkdir(parents=True)
        (datad / user / "cache").mkdir(parents=True)
        (datad / user / "cache" / "thumb.bin").write_bytes(os.urandom(100))
    (datad / "appdata_x1" / "preview").mkdir(parents=True)
    for i in range(50):
        (datad / "appdata_x1" / "preview" / f"{i}.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + os.urandom(64)
        )
    for i in range(photos):
        shot = when - timedelta(hours=i * 2, minutes=7)
        p = datad / ("alice" if i % 3 else "bob") / "files" / "Photos" / f"IMG_{i:04d}.jpg"
        p.write_bytes(jpeg(4032, 3024, shot - timedelta(days=1)))
        set_mtime(p, shot)
    for i in range(docs):
        d_when = when - timedelta(days=i)
        p = datad / "alice" / "files" / "Documents" / f"contract-{i}.odt"
        odt(p, f"Contract {i}")
        set_mtime(p, d_when)
    vid = datad / "bob" / "files" / "Photos" / "VID_0001.mp4"
    vid.write_bytes(mp4(12.5))
    set_mtime(vid, when - timedelta(hours=5))
    git_repo(datad / "alice" / "files" / "Projects" / "tool", when - timedelta(days=2))
    (datad / "alice" / "files" / "notes.txt").write_text("Shopping list\n")
    set_mtime(datad / "alice" / "files" / "notes.txt", when - timedelta(minutes=30))
    (datad / "alice" / "files" / "corrupt.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
    set_mtime(datad / "alice" / "files" / "corrupt.jpg", when - timedelta(minutes=10))
    csv = [
        (f"/var/www/{app}", f"apps/{app}/backup/var/www/{app}"),
        (f"/home/yunohost.app/{app}", f"apps/{app}/backup/home/yunohost.app/{app}"),
        (f"/etc/yunohost/apps/{app}", f"apps/{app}/settings"),
    ]
    _write_meta(
        appdir,
        when,
        csv,
        apps={app: {"version": "1.0~ynh1", "name": "Filebox", "description": "synthetic"}},
        system={},
    )
    dirs[f"auto_{app}"] = appdir
    return dirs


def _write_meta(
    root: Path, when: datetime, csv: list[tuple[str, str]], apps: dict, system: dict
) -> None:
    size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    info = {
        "description": "synthetic",
        "created_at": int(when.timestamp()),
        "size": size,
        "size_details": {"system": {k: 0 for k in system}, "apps": {k: size for k in apps}},
        "apps": apps,
        "system": system,
        "from_yunohost_version": "12.1.41",
    }
    (root / "info.json").write_text(json.dumps(info))
    (root / "backup.csv").write_text("".join(f'"{s}","{d}"\n' for s, d in csv))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument(
        "--scenario", default="normal", choices=["normal", "shrink", "grow", "missing", "emptydb"]
    )
    ap.add_argument("--generations", type=int, default=3)
    ap.add_argument("--app", default="filebox")
    ap.add_argument("--passphrase", default="synthetic-passphrase")
    ap.add_argument("--domain", default="synthetic.test")
    ap.add_argument(
        "--start", default=None, help="timestamp of the newest generation (ISO), default now"
    )
    ns = ap.parse_args()
    borg = os.environ.get("BORG") or shutil.which("borg")
    if not borg:
        print("borg binary not found (set BORG=...)", file=sys.stderr)
        return 2
    repo = Path(ns.repo).resolve()
    env = dict(
        os.environ,
        BORG_PASSPHRASE=ns.passphrase,
        BORG_REPO=str(repo),
        BORG_RELOCATED_REPO_ACCESS_IS_OK="yes",
        BORG_HOSTNAME="synthetic",
    )
    repo.parent.mkdir(parents=True, exist_ok=True)
    if not repo.exists():
        subprocess.run([borg, "init", "-e", "repokey", str(repo)], env=env, check=True)
    newest = datetime.fromisoformat(ns.start) if ns.start else datetime.now().replace(microsecond=0)
    work = Path(os.environ.get("TMPDIR", "/tmp")) / f"bbic-synth-{int(time.time())}"
    for gen in range(ns.generations):
        when = newest - timedelta(days=ns.generations - 1 - gen)
        dirs = build_workdir(work, ns.app, when, ns.scenario, gen, ns.generations, ns.domain)
        for component, directory in dirs.items():
            if (
                ns.scenario == "missing"
                and gen == ns.generations - 1
                and component == f"auto_{ns.app}"
            ):
                continue
            local_when = when + timedelta(minutes=list(dirs).index(component) * 3)
            name = f"{component}-{local_when.strftime('%Y-%m-%dT%H:%M:%S')}"
            # borg --timestamp expects UTC; archive names mirror borg_ynh's {now} (local time).
            utc_when = local_when.astimezone().astimezone(UTC)
            proc = subprocess.run(
                [
                    borg,
                    "create",
                    "--timestamp",
                    utc_when.strftime("%Y-%m-%dT%H:%M:%S"),
                    f"::{name}",
                    ".",
                ],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode >= 2:
                print(proc.stderr, file=sys.stderr)
                raise SystemExit(f"borg create failed (rc={proc.returncode})")
            print("created", name)
    shutil.rmtree(work, ignore_errors=True)
    print(
        json.dumps(
            {
                "repository": str(repo),
                "passphrase": ns.passphrase,
                "app": ns.app,
                "domain": ns.domain,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

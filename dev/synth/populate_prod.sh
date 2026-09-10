#!/bin/bash
# Runs ON the production VM: create recognisable synthetic user data for installed apps and mail.
set -Eeuo pipefail
domain=$(cat /etc/yunohost/current_host)
python3 - <<'PY'
import os, struct, zipfile, random, time
from datetime import datetime, timedelta
from pathlib import Path

def jpeg(width, height, when):
    dt = when.strftime("%Y:%m:%d %H:%M:%S").encode() + b"\x00"
    tiff = bytearray(b"II*\x00" + struct.pack("<I", 8)); n = 2; data_area = 8 + 2 + n * 12 + 4
    ifd = struct.pack("<H", n) + struct.pack("<HHII", 0x0132, 2, len(dt), data_area)
    exif_off = data_area + len(dt)
    ifd += struct.pack("<HHII", 0x8769, 4, 1, exif_off) + struct.pack("<I", 0)
    tiff += ifd + dt
    tiff += struct.pack("<H", 1) + struct.pack("<HHII", 0x9003, 2, len(dt), exif_off + 2 + 12 + 4) + struct.pack("<I", 0) + dt
    app1 = b"Exif\x00\x00" + bytes(tiff)
    sof0 = struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    filler = os.urandom(300 * 1024)
    return b"\xff\xd8\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xc0" + sof0 + b"\xff\xd9" + filler

now = datetime.now()
for app_dir in Path("/home/yunohost.app").iterdir():
    if not app_dir.is_dir() or app_dir.name.startswith("."):
        continue
    app = app_dir.name
    import pwd
    try:
        pw = pwd.getpwnam(app)
    except KeyError:
        continue
    base = app_dir
    for user in ("alice", "bbicadmin"):
        photos = base / user / "Photos"; docs = base / user / "Documents"
        photos.mkdir(parents=True, exist_ok=True); docs.mkdir(parents=True, exist_ok=True)
        for i in range(40):
            when = now - timedelta(hours=i * 5, minutes=11)
            p = photos / f"IMG_{i:04d}.jpg"
            p.write_bytes(jpeg(4032, 3024, when - timedelta(days=1)))
            os.utime(p, (when.timestamp(), when.timestamp()))
        for i in range(12):
            when = now - timedelta(days=i, hours=2)
            p = docs / f"contract-{i}.odt"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("mimetype", "application/vnd.oasis.opendocument.text"); zf.writestr("content.xml", f"<x>Contract {i}</x>"); zf.writestr("f.bin", os.urandom(20000))
            os.utime(p, (when.timestamp(), when.timestamp()))
    for path in base.rglob("*"):
        os.chown(path, pw.pw_uid, pw.pw_gid)
    print(f"populated {base}")
PY
# Mails through the real MTA so Dovecot indexes them.
for i in $(seq 1 45); do
    subj=$(printf '%s #%d' "$(echo -e "Re: Tuesday meeting\nYour invoice is ready\nWeekend plans\nPhotos from the trip\nServer maintenance window" | sed -n "$((i % 5 + 1))p")" "$i")
    printf 'From: Alice Example <alice@%s>\nTo: bbicadmin@%s\nSubject: %s\nMessage-ID: <synthetic-%d@%s>\n\nBody of message %d (never shown in reports).\n' "$domain" "$domain" "$subj" "$i" "$domain" "$i" | sendmail -t
    printf 'From: Bob <bob@example.org>\nTo: alice@%s\nSubject: Hello Alice #%d\nMessage-ID: <synthetic-a-%d@%s>\n\nHi.\n' "$domain" "$i" "$i" "$domain" | sendmail -t
done
sleep 8
echo "mails delivered: $(find /var/mail -type f -path '*/new/*' -o -type f -path '*/cur/*' 2>/dev/null | wc -l)"

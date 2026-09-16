import struct
import zipfile
from datetime import UTC, datetime, timezone

from borg_backup_integrity_check.evidence.extractors import EvidenceExtractor
from borg_backup_integrity_check.evidence.sniff import sniff_bytes, sniff_path

EMAIL = b"""Return-Path: <alice@example.org>
Delivered-To: bob@example.org
From: Alice Example <alice@example.org>
To: bob@example.org
Subject: =?UTF-8?Q?Re=3A_Tuesday_meeting_=E2=98=95?=
Date: Thu, 10 Sep 2026 08:31:00 +0200
Message-ID: <abc123@example.org>
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

Body text that must never appear in a report.
"""


def _jpeg_with_exif(width=640, height=480, dt=b"2026:09:09 17:41:00"):
    # Minimal TIFF (little endian) with IFD0 -> DateTime tag 0x0132 and Exif pointer -> DateTimeOriginal.
    tiff = bytearray(b"II*\x00" + struct.pack("<I", 8))
    # IFD0 with two entries: 0x0132 (DateTime) and 0x8769 (Exif IFD pointer)
    ifd0_offset = 8
    n_entries = 2
    ifd0 = struct.pack("<H", n_entries)
    data_area = ifd0_offset + 2 + n_entries * 12 + 4
    dt_bytes = dt + b"\x00"
    ifd0 += struct.pack("<HHII", 0x0132, 2, len(dt_bytes), data_area)
    exif_ifd_offset = data_area + len(dt_bytes)
    ifd0 += struct.pack("<HHII", 0x8769, 4, 1, exif_ifd_offset)
    ifd0 += struct.pack("<I", 0)
    tiff += ifd0 + dt_bytes
    orig = b"2026:09:09 17:41:05\x00"
    exif_ifd = (
        struct.pack("<H", 1)
        + struct.pack("<HHII", 0x9003, 2, len(orig), exif_ifd_offset + 2 + 12 + 4)
        + struct.pack("<I", 0)
        + orig
    )
    tiff += exif_ifd
    app1 = b"Exif\x00\x00" + bytes(tiff)
    sof0 = struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return (
        b"\xff\xd8"
        + b"\xff\xe1"
        + struct.pack(">H", len(app1) + 2)
        + app1
        + b"\xff\xc0"
        + sof0
        + b"\xff\xd9"
    )


def _mp4(duration=12.5, timescale=1000):
    mvhd_body = (
        b"\x00"
        + b"\x00\x00\x00"
        + b"\x00" * 8
        + struct.pack(">I", timescale)
        + struct.pack(">I", int(duration * timescale))
        + b"\x00" * 80
    )
    mvhd = struct.pack(">I4s", 8 + len(mvhd_body), b"mvhd") + mvhd_body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp_body = b"isom\x00\x00\x02\x00isomiso2mp41"
    ftyp = struct.pack(">I4s", 8 + len(ftyp_body), b"ftyp") + ftyp_body
    return ftyp + moov


def test_sniff_signatures():
    assert sniff_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20, "x.jpg").mime == "image/jpeg"
    assert sniff_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20).mime == "image/png"
    assert sniff_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ").mime == "image/webp"
    assert sniff_bytes(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00").mime == "image/heic"
    assert sniff_bytes(b"\x00\x00\x00\x18ftypqt  \x00\x00\x00\x00").kind == "video"
    assert sniff_bytes(EMAIL).kind == "email"
    assert sniff_bytes(b"%PDF-1.7\n").kind == "document"
    assert sniff_bytes(b"hello world\n", "notes.txt").kind == "text"
    assert sniff_bytes(b"\x00\x01\x02\x03binary", "blob.bin").kind == "binary"
    assert sniff_bytes(b"").kind == "empty"
    assert sniff_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 8).mime == "video/x-matroska"


def test_email_evidence_has_subject_date_sender_but_no_body(tmp_path):
    path = tmp_path / "1694332260.M1P1.host,S=812,W=830:2,S"
    path.write_bytes(EMAIL)
    ev = EvidenceExtractor().describe(path)
    assert ev.kind == "email" and ev.readable
    assert ev.title == "Re: Tuesday meeting ☕"
    assert ev.when == datetime(
        2026, 9, 10, 8, 31, tzinfo=timezone(__import__("datetime").timedelta(hours=2))
    )
    assert ev.when_source == "header_date"
    assert ev.details["from"] == "Alice Example"
    assert ev.details["message_id"] == "<abc123@example.org>"
    assert "Body text" not in str(ev.to_dict())


def test_mbox_from_line_and_malformed_email(tmp_path):
    path = tmp_path / "msg"
    path.write_bytes(b"From alice@example.org Thu Sep 10 08:31:00 2026\n" + EMAIL)
    assert EvidenceExtractor().describe(path).title.startswith("Re: Tuesday")
    bad = tmp_path / "bad.eml"
    bad.write_bytes(b"Subject: only\n\n")
    ev = EvidenceExtractor().describe(bad)
    assert ev.kind == "email" and ev.title == "only" and ev.when_source == "mtime"


def test_image_evidence_dimensions_and_exif(tmp_path):
    path = tmp_path / "IMG_4821.jpg"
    path.write_bytes(_jpeg_with_exif())
    ev = EvidenceExtractor().describe(path)
    assert ev.kind == "image" and ev.mime == "image/jpeg" and ev.readable
    assert (ev.details["width"], ev.details["height"]) == (640, 480)
    assert ev.when == datetime(2026, 9, 9, 17, 41, 5) and ev.when_source == "exif"
    png = tmp_path / "shot.png"
    png.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", 1920, 1080)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00" * 8
    )
    ev = EvidenceExtractor().describe(png)
    assert (ev.details["width"], ev.details["height"]) == (1920, 1080) and ev.when_source == "mtime"


def test_video_duration_from_mvhd(tmp_path):
    path = tmp_path / "VID_9157.mp4"
    path.write_bytes(_mp4(12.5))
    ev = EvidenceExtractor().describe(path)
    assert ev.kind == "video" and ev.details["duration_seconds"] == 12.5


def test_office_and_odf_documents(tmp_path):
    docx = tmp_path / "contract.docx"
    with zipfile.ZipFile(docx, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w/>")
    assert sniff_path(docx).mime.endswith("wordprocessingml.document")
    odt = tmp_path / "design.odt"
    with zipfile.ZipFile(odt, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("content.xml", "<x/>")
    ev = EvidenceExtractor().describe(odt)
    assert ev.kind == "document"
    plain = tmp_path / "archive.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("a.txt", "x")
    assert sniff_path(plain).kind == "archive"


def test_git_repo_evidence(tmp_path):
    repo = tmp_path / "tool"
    (repo / ".git" / "logs").mkdir(parents=True)
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / ".git" / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    (repo / ".git" / "logs" / "HEAD").write_text(
        "0" * 40 + " " + "b" * 40 + " Alice <a@x> 1757500000 +0200\tcommit: Fix the thing\n"
    )
    ev = EvidenceExtractor().describe(repo, kind_hint="git_repo")
    assert ev.kind == "git_repo" and ev.readable
    assert ev.details["branch"] == "main" and ev.details["last_commit"] == "b" * 12
    assert ev.details["last_message"] == "commit: Fix the thing" and ev.when_source == "commit"
    assert ev.when == datetime.fromtimestamp(1757500000, tz=UTC)


def test_unreadable_missing_and_empty_objects(tmp_path):
    ev = EvidenceExtractor().describe(tmp_path / "missing.bin")
    assert not ev.readable and ev.error == "not extracted"
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    ev = EvidenceExtractor().describe(empty)
    assert not ev.readable and ev.error == "empty file"
    corrupt = tmp_path / "corrupt.jpg"
    corrupt.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
    ev = EvidenceExtractor().describe(corrupt)
    assert ev.kind == "image" and ev.readable and "width" not in ev.details


def test_a_message_whose_first_header_is_dkim_or_arc_is_still_mail():
    """Real deliveries put ARC/DKIM/Authentication-Results first; the old rule only saw Return-Path."""
    message = (
        b"ARC-Seal: i=1; a=rsa-sha256; t=1789539425; cv=none;\n"
        b"Authentication-Results: opensourceit.org; dkim=pass\n"
        b"DKIM-Signature: v=1; a=rsa-sha256; d=example.org;\n"
        b"From: Alice <alice@example.org>\n"
        b"Subject: Weekend plans\n"
        b"Date: Tue, 15 Sep 2026 23:17:00 +0200\n"
        b"\nbody\n"
    )
    assert sniff_bytes(message, "1789539425.M486049P3134941,S=84609:2,").kind == "email"
    assert sniff_bytes(b"Return-Path: <a@b>\nSubject: x\n\nbody\n").kind == "email"
    assert sniff_bytes(b"X-Spam: no\nX-Other: 1\n\nnot a message\n").kind != "email"
    assert sniff_bytes(b"just some text\nwith lines\n").kind != "email"


def test_content_alarm_flags_encrypted_looking_objects(tmp_path):
    """A ransomware rewrite keeps the name; the bytes stop matching it and stop compressing."""
    import os

    from borg_backup_integrity_check.evidence.extractors import EvidenceExtractor
    from borg_backup_integrity_check.evidence.sniff import sniff_path
    from borg_backup_integrity_check.evidence.tampering import content_alarm

    def alarm(path):
        return content_alarm(path, sniff_path(path), path.stat().st_size)

    encrypted = tmp_path / "IMG_2031.jpg"
    encrypted.write_bytes(os.urandom(256 * 1024))
    genuine = tmp_path / "IMG_2032.jpg"
    genuine.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200_000 + b"\xff\xd9")
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"plain words repeated many times.\n" * 500)
    opaque = tmp_path / "89f258347a755a8b2ecdbb6abdefae8893c348"  # a git object: no extension
    opaque.write_bytes(os.urandom(64 * 1024))

    assert "content is not image" in alarm(encrypted) and "encrypted?" in alarm(encrypted)
    assert alarm(genuine) is None
    assert alarm(notes) is None
    assert alarm(opaque) is None, "unidentified but unnamed objects must not raise noise"

    assert EvidenceExtractor().describe(encrypted).details.get("content_alarm")
    assert EvidenceExtractor().describe(genuine).details.get("content_alarm") is None

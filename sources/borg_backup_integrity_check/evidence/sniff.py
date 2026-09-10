"""Content-type detection from file signatures, structure and extensions (no libmagic needed)."""

from __future__ import annotations

import mimetypes
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

HEADER_BYTES = 8192

_SIGNATURES: list[tuple[bytes, int, str, str]] = [
    # (magic, offset, kind, mime)
    (b"\xff\xd8\xff", 0, "image", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", 0, "image", "image/png"),
    (b"GIF87a", 0, "image", "image/gif"),
    (b"GIF89a", 0, "image", "image/gif"),
    (b"II*\x00", 0, "image", "image/tiff"),
    (b"MM\x00*", 0, "image", "image/tiff"),
    (b"BM", 0, "image", "image/bmp"),
    (b"%PDF-", 0, "document", "application/pdf"),
    (b"\x1a\x45\xdf\xa3", 0, "video", "video/x-matroska"),
    (b"OggS", 0, "audio", "audio/ogg"),
    (b"fLaC", 0, "audio", "audio/flac"),
    (b"ID3", 0, "audio", "audio/mpeg"),
    (b"\x1f\x8b", 0, "archive", "application/gzip"),
    (b"7z\xbc\xaf\x27\x1c", 0, "archive", "application/x-7z-compressed"),
    (b"Rar!\x1a\x07", 0, "archive", "application/vnd.rar"),
    (b"\xfd7zXZ\x00", 0, "archive", "application/x-xz"),
    (b"BZh", 0, "archive", "application/x-bzip2"),
    (b"SQLite format 3\x00", 0, "database", "application/vnd.sqlite3"),
    (b"\x7fELF", 0, "binary", "application/x-executable"),
]

_EMAIL_HEADER_RE = re.compile(
    rb"^(From |(?:Received|Return-Path|Delivered-To|From|To|Subject|Date|Message-ID|MIME-Version|X-[A-Za-z0-9-]+):)",
    re.I,
)


@dataclass(frozen=True)
class ContentType:
    kind: str
    mime: str | None = None
    detail: str | None = None


def read_header(path: Path, size: int = HEADER_BYTES) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(size)


def sniff_bytes(header: bytes, name: str = "") -> ContentType:
    if not header:
        return ContentType("empty", None)
    for magic, offset, kind, mime in _SIGNATURES:
        if header[offset : offset + len(magic)] == magic:
            return ContentType(kind, mime)
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ContentType("image", "image/webp")
    if header[:4] == b"RIFF" and header[8:12] == b"AVI ":
        return ContentType("video", "video/x-msvideo")
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return ContentType("audio", "audio/x-wav")
    if header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"heif"):
            return ContentType("image", "image/heic")
        if brand in (b"avif", b"avis"):
            return ContentType("image", "image/avif")
        if brand.startswith(b"qt"):
            return ContentType("video", "video/quicktime")
        if brand in (b"M4A ", b"M4B "):
            return ContentType("audio", "audio/mp4")
        return ContentType("video", "video/mp4")
    if header[:2] == b"PK":
        return _sniff_zip(name, header)
    if _EMAIL_HEADER_RE.match(header):
        return ContentType("email", "message/rfc822")
    if header.startswith(b"{\\rtf"):
        return ContentType("document", "application/rtf")
    if header.startswith(b"\xd0\xcf\x11\xe0"):
        return ContentType("document", "application/msword")
    lowered = header[:512].lower()
    if lowered.lstrip().startswith((b"<!doctype html", b"<html")):
        return ContentType("text", "text/html")
    if lowered.startswith(b"<?xml"):
        return ContentType("text", "application/xml")
    if _is_probably_text(header):
        mime, _ = mimetypes.guess_type(name)
        if mime and mime.startswith("text/"):
            return ContentType("text", mime)
        if name.lower().endswith(
            (
                ".md",
                ".txt",
                ".csv",
                ".json",
                ".yml",
                ".yaml",
                ".ini",
                ".cfg",
                ".log",
                ".vcf",
                ".ics",
            )
        ):
            return ContentType("text", mime or "text/plain")
        return ContentType("text", "text/plain")
    mime, _ = mimetypes.guess_type(name)
    if mime:
        return ContentType(_kind_from_mime(mime), mime)
    return ContentType("binary", "application/octet-stream")


def sniff_path(path: Path) -> ContentType:
    try:
        header = read_header(path)
    except OSError as exc:
        return ContentType("unreadable", None, str(exc))
    ct = sniff_bytes(header, path.name)
    if ct.mime in ("application/zip", None) and ct.kind in ("archive", "binary"):
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
        except (zipfile.BadZipFile, OSError):
            return ct
        return _classify_zip(names, path.name)
    return ct


def _sniff_zip(name: str, header: bytes) -> ContentType:
    mime, _ = mimetypes.guess_type(name)
    lowered = name.lower()
    if lowered.endswith((".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub")):
        return ContentType("document", mime or "application/zip")
    if b"mimetypeapplication/vnd.oasis.opendocument" in header[:100]:
        return ContentType("document", "application/vnd.oasis.opendocument")
    return ContentType("archive", "application/zip")


def _classify_zip(names: set[str], filename: str) -> ContentType:
    if "[Content_Types].xml" in names:
        if any(n.startswith("word/") for n in names):
            return ContentType(
                "document",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        if any(n.startswith("xl/") for n in names):
            return ContentType(
                "document", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        if any(n.startswith("ppt/") for n in names):
            return ContentType(
                "document",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
    if "mimetype" in names and "content.xml" in names:
        return ContentType("document", "application/vnd.oasis.opendocument")
    if "META-INF/container.xml" in names:
        return ContentType("document", "application/epub+zip")
    return ContentType("archive", "application/zip")


def _kind_from_mime(mime: str) -> str:
    major = mime.split("/")[0]
    if major in ("image", "video", "audio", "text"):
        return major
    if (
        mime in ("application/pdf", "application/msword")
        or "document" in mime
        or "opendocument" in mime
    ):
        return "document"
    if mime in ("application/zip", "application/gzip", "application/x-tar"):
        return "archive"
    return "binary"


def _is_probably_text(sample: bytes) -> bool:
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        pass
    printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
    return printable / max(len(sample), 1) > 0.9

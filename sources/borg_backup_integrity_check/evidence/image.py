"""Image evidence: dimensions and capture date from EXIF, using pure-Python parsers.

Pillow is used opportunistically when available (broader format support), but
the built-in JPEG/PNG/GIF/WebP parsers keep the feature dependency-free.
"""

from __future__ import annotations

import struct
from datetime import datetime
from pathlib import Path

from .models import Evidence

EXIF_DATETIME_ORIGINAL = 0x9003
EXIF_DATETIME = 0x0132
EXIF_IFD_POINTER = 0x8769


def extract_image(
    path: Path, mime: str | None, size: int | None, mtime: datetime | None
) -> Evidence:
    details: dict = {}
    when, when_source = mtime, "mtime"
    error = None
    try:
        with open(path, "rb") as fh:
            head = fh.read(512 * 1024)
        dims = _dimensions(head, mime)
        if dims:
            details["width"], details["height"] = dims
        exif_dt = _exif_datetime(head) if mime == "image/jpeg" else None
        if exif_dt is None:
            exif_dt = _pillow_exif_datetime(path)
        if exif_dt:
            when, when_source = exif_dt, "exif"
        if not dims:
            pil_dims = _pillow_dimensions(path)
            if pil_dims:
                details["width"], details["height"] = pil_dims
    except OSError as exc:
        error = str(exc)
    readable = error is None and (bool(details) or mime is not None)
    return Evidence(
        kind="image",
        title=path.name,
        when=when,
        when_source=when_source,
        mime=mime,
        size=size,
        path=str(path),
        details=details,
        readable=readable,
        error=error,
    )


def _dimensions(head: bytes, mime: str | None) -> tuple[int, int] | None:
    try:
        if mime == "image/png" and head[12:16] == b"IHDR":
            w, h = struct.unpack(">II", head[16:24])
            return w, h
        if mime == "image/gif":
            w, h = struct.unpack("<HH", head[6:10])
            return w, h
        if mime == "image/jpeg":
            return _jpeg_dimensions(head)
        if mime == "image/webp":
            return _webp_dimensions(head)
        if mime == "image/bmp":
            w, h = struct.unpack("<ii", head[18:26])
            return abs(w), abs(h)
    except (struct.error, IndexError):
        return None
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        i += 2 + length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    if chunk == b"VP8L":
        b = data[21:25]
        w = ((b[1] & 0x3F) << 8 | b[0]) + 1
        h = ((b[3] & 0x0F) << 10 | b[2] << 2 | (b[1] & 0xC0) >> 6) + 1
        return w, h
    if chunk == b"VP8 ":
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    return None


def _exif_datetime(data: bytes) -> datetime | None:
    """Locate the APP1 Exif segment in a JPEG and read DateTimeOriginal (or DateTime)."""
    i = 2
    while i + 4 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker == 0xE1 and data[i + 4 : i + 10] == b"Exif\x00\x00":
            return _parse_tiff(data[i + 10 : i + 2 + length])
        if marker == 0xDA:
            return None
        i += 2 + length
    return None


def _parse_tiff(tiff: bytes) -> datetime | None:
    if len(tiff) < 8:
        return None
    endian = "<" if tiff[:2] == b"II" else ">" if tiff[:2] == b"MM" else None
    if endian is None:
        return None
    try:
        ifd0 = struct.unpack(endian + "I", tiff[4:8])[0]
        found: dict[int, str] = {}
        _read_ifd(tiff, ifd0, endian, found, depth=0)
        raw = found.get(EXIF_DATETIME_ORIGINAL) or found.get(EXIF_DATETIME)
    except (struct.error, IndexError, ValueError):
        return None
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _read_ifd(tiff: bytes, offset: int, endian: str, found: dict[int, str], depth: int) -> None:
    if depth > 2 or offset + 2 > len(tiff):
        return
    count = struct.unpack(endian + "H", tiff[offset : offset + 2])[0]
    for n in range(min(count, 512)):
        entry = offset + 2 + n * 12
        if entry + 12 > len(tiff):
            return
        tag, typ, num = struct.unpack(endian + "HHI", tiff[entry : entry + 8])
        if tag == EXIF_IFD_POINTER:
            sub = struct.unpack(endian + "I", tiff[entry + 8 : entry + 12])[0]
            _read_ifd(tiff, sub, endian, found, depth + 1)
        elif tag in (EXIF_DATETIME_ORIGINAL, EXIF_DATETIME) and typ == 2:
            value_offset = (
                struct.unpack(endian + "I", tiff[entry + 8 : entry + 12])[0]
                if num > 4
                else entry + 8
            )
            found[tag] = tiff[value_offset : value_offset + num].decode("ascii", "replace")


def _pillow_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:  # noqa: BLE001 - PIL raises many types for corrupt images
        return None


def _pillow_exif_datetime(path: Path) -> datetime | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            raw = exif.get(EXIF_DATETIME_ORIGINAL) or exif.get(EXIF_DATETIME)
            if not raw:
                ifd = exif.get_ifd(EXIF_IFD_POINTER)
                raw = ifd.get(EXIF_DATETIME_ORIGINAL) if ifd else None
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None

"""Video/audio evidence: container type and duration (ISO-BMFF ``mvhd`` parsed natively, ffprobe when present)."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from datetime import datetime
from pathlib import Path

from .models import Evidence


def extract_media(
    path: Path, kind: str, mime: str | None, size: int | None, mtime: datetime | None
) -> Evidence:
    details: dict = {}
    error = None
    try:
        duration = (
            _mp4_duration(path) if mime in ("video/mp4", "video/quicktime", "audio/mp4") else None
        )
        if duration is None:
            probe = _ffprobe(path)
            if probe:
                duration = probe.get("duration")
                if probe.get("width"):
                    details["width"], details["height"] = probe["width"], probe.get("height")
        if duration is not None:
            details["duration_seconds"] = round(duration, 1)
    except OSError as exc:
        error = str(exc)
    return Evidence(
        kind=kind,
        title=path.name,
        when=mtime,
        mime=mime,
        size=size,
        path=str(path),
        details=details,
        readable=error is None,
        error=error,
    )


def _mp4_duration(path: Path) -> float | None:
    """Walk top-level boxes to ``moov``/``mvhd`` and compute duration = duration/timescale."""
    with open(path, "rb") as fh:
        data = fh.read(4 * 1024 * 1024)
    pos = 0
    while pos + 8 <= len(data):
        box_size, box_type = struct.unpack(">I4s", data[pos : pos + 8])
        header = 8
        if box_size == 1 and pos + 16 <= len(data):
            box_size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            header = 16
        if box_size < header:
            return None
        if box_type == b"moov":
            return _find_mvhd(data[pos + header : pos + box_size])
        pos += box_size
    return None


def _find_mvhd(moov: bytes) -> float | None:
    pos = 0
    while pos + 8 <= len(moov):
        size, typ = struct.unpack(">I4s", moov[pos : pos + 8])
        if size < 8:
            return None
        if typ == b"mvhd":
            version = moov[pos + 8]
            if version == 1:
                timescale = struct.unpack(">I", moov[pos + 28 : pos + 32])[0]
                duration = struct.unpack(">Q", moov[pos + 32 : pos + 40])[0]
            else:
                timescale = struct.unpack(">I", moov[pos + 20 : pos + 24])[0]
                duration = struct.unpack(">I", moov[pos + 24 : pos + 28])[0]
            return duration / timescale if timescale else None
        pos += size
    return None


def _ffprobe(path: Path) -> dict | None:
    binary = shutil.which("ffprobe")
    if not binary:
        return None
    try:
        out = subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        data = json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None
    result: dict = {}
    fmt = data.get("format", {})
    if fmt.get("duration"):
        result["duration"] = float(fmt["duration"])
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            result["width"], result["height"] = stream.get("width"), stream.get("height")
            break
    return result

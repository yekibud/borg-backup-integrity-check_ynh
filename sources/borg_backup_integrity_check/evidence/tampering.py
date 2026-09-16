"""Does a sampled object still look like what it claims to be?

Ransomware rewrites files in place, often keeping their names. A backup of encrypted files is a
perfectly valid backup, so integrity checks at the Borg level cannot see it; what gives it away is
the content: a `.jpg` that is not an image, and bytes that will not compress.

Both checks are generic (no per-application rule) and reuse bytes the evidence layer already reads.
"""

from __future__ import annotations

import zlib
from pathlib import Path

from .sniff import ContentType, read_header, sniff_bytes

# What the name promises, when the name is worth anything.
EXPECTED_KIND_BY_SUFFIX = {
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".gif": "image",
    ".webp": "image",
    ".heic": "image",
    ".tif": "image",
    ".tiff": "image",
    ".mp4": "video",
    ".mov": "video",
    ".mkv": "video",
    ".avi": "video",
    ".mp3": "audio",
    ".flac": "audio",
    ".ogg": "audio",
    ".m4a": "audio",
    ".pdf": "document",
    ".odt": "document",
    ".ods": "document",
    ".odp": "document",
    ".docx": "document",
    ".xlsx": "document",
    ".pptx": "document",
    ".doc": "document",
    ".rtf": "document",
    ".eml": "email",
    ".zip": "archive",
    ".gz": "archive",
    ".xz": "archive",
    ".7z": "archive",
    ".sql": "text",
    ".txt": "text",
    ".md": "text",
    ".json": "text",
    ".xml": "text",
    ".html": "text",
    ".csv": "text",
}
UNIDENTIFIED_KINDS = frozenset({"binary", "unknown", "file"})
PROBE_BYTES = 8192
MIN_SIZE_FOR_ENTROPY = 4096
INCOMPRESSIBLE_RATIO = 0.98


def read_probe(path: Path, size: int = PROBE_BYTES, offset: int = 0) -> bytes:
    """Read a slice of a file; the middle matters because some ransomware keeps the header."""
    try:
        with open(path, "rb") as fh:
            if offset:
                fh.seek(offset)
            return fh.read(size)
    except OSError:
        return b""


def looks_incompressible(path: Path, size: int) -> bool:
    """Encrypted (or already compressed) bytes do not shrink; sample the start and the middle."""
    if size < MIN_SIZE_FOR_ENTROPY:
        return False
    samples = [read_probe(path)]
    if size > 4 * PROBE_BYTES:
        samples.append(read_probe(path, offset=(size // 2) - (PROBE_BYTES // 2)))
    data = b"".join(samples)
    if len(data) < MIN_SIZE_FOR_ENTROPY:
        return False
    return len(zlib.compress(data, 1)) / len(data) > INCOMPRESSIBLE_RATIO


def content_alarm(path: Path, content: ContentType, size: int | None) -> str | None:
    """Why this object no longer looks like itself, or None when nothing is suspicious.

    ``content`` comes from a sniff that falls back to the file name, which is exactly what must not
    be trusted here: the name is what ransomware keeps. So the bytes are classified again on their
    own, without the name.
    """
    expected = EXPECTED_KIND_BY_SUFFIX.get(path.suffix.lower())
    by_content = sniff_bytes(read_header(path), "") if expected else content
    identified = by_content.kind not in UNIDENTIFIED_KINDS
    if expected and not identified:
        reason = f"named {path.suffix.lower()} but its content is not {expected}"
        if looks_incompressible(path, size or 0):
            return reason + " and does not compress (encrypted?)"
        return reason
    # Deliberately no alarm on "unidentified and incompressible" alone: git objects, packfiles,
    # GnuPG files and password vaults are all exactly that, and the noise would bury the signal.
    # An attack that renames what it encrypts shows up in the manifest comparison instead.
    return None

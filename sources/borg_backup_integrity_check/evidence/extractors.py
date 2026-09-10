"""EvidenceExtractor: dispatch a sampled object to the right generic parser."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .email import extract_email
from .git import extract_git_repo
from .image import extract_image
from .models import Evidence
from .sniff import sniff_path
from .video import extract_media

READ_PROBE_BYTES = 64 * 1024


class EvidenceExtractor:
    """Recognises what a retrieved object is and produces recognisable, safe evidence."""

    def __init__(self, include_sender: bool = True) -> None:
        self.include_sender = include_sender

    def describe(
        self,
        path: Path,
        kind_hint: str = "file",
        mtime: datetime | None = None,
        size: int | None = None,
        display_path: str | None = None,
    ) -> Evidence:
        if kind_hint == "git_repo":
            evidence = extract_git_repo(path, mtime, size)
            evidence.path = display_path or evidence.path
            return evidence
        if not path.exists():
            return Evidence(
                kind="unknown",
                title=path.name,
                when=mtime,
                size=size,
                path=display_path or str(path),
                readable=False,
                error="not extracted",
            )
        try:
            stat = path.stat()
        except OSError as exc:
            return Evidence(
                kind="unknown",
                title=path.name,
                when=mtime,
                size=size,
                path=display_path or str(path),
                readable=False,
                error=str(exc),
            )
        size = stat.st_size if size is None else size
        if mtime is None:
            mtime = datetime.fromtimestamp(stat.st_mtime)
        if not _readable(path):
            return Evidence(
                kind="unknown",
                title=path.name,
                when=mtime,
                size=size,
                path=display_path or str(path),
                readable=False,
                error="cannot read file",
            )
        ct = sniff_path(path)
        if ct.kind == "email":
            evidence = extract_email(path, size, mtime, self.include_sender)
        elif ct.kind == "image":
            evidence = extract_image(path, ct.mime, size, mtime)
        elif ct.kind in ("video", "audio"):
            evidence = extract_media(path, ct.kind, ct.mime, size, mtime)
        elif ct.kind == "unreadable":
            evidence = Evidence(
                kind="unknown",
                title=path.name,
                when=mtime,
                size=size,
                readable=False,
                error=ct.detail,
            )
        elif ct.kind == "empty":
            evidence = Evidence(
                kind="file", title=path.name, when=mtime, size=0, readable=False, error="empty file"
            )
        else:
            evidence = Evidence(kind=ct.kind, title=path.name, when=mtime, mime=ct.mime, size=size)
        evidence.path = display_path or str(path)
        return evidence


def _readable(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.read(READ_PROBE_BYTES)
        return True
    except OSError:
        return False

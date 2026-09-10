"""Typed views over Borg's JSON output (``borg list --json``, ``--json-lines``, ``borg info --json``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def parse_borg_time(value: str | None) -> datetime | None:
    """Borg emits naive local ISO-8601 timestamps, optionally with microseconds."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class ArchiveRef:
    """One archive as listed by ``borg list --json`` (repository level)."""

    name: str
    id: str
    start: datetime

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ArchiveRef:
        start = parse_borg_time(data.get("start") or data.get("time"))
        if start is None:
            raise ValueError(f"archive {data.get('name')!r} has no parseable start time")
        return cls(name=data["name"], id=data.get("id", ""), start=start)


@dataclass(frozen=True, slots=True)
class ArchiveStats:
    """Statistics from ``borg info --json`` for one archive.

    ``original_size`` is the logical (uncompressed, non-deduplicated) size of all files,
    ``compressed_size`` the size after compression before deduplication and
    ``deduplicated_size`` the space this archive uniquely adds to the repository.
    """

    original_size: int
    compressed_size: int
    deduplicated_size: int
    nfiles: int
    duration: float | None = None
    hostname: str | None = None
    command_line: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, archive: dict[str, Any]) -> ArchiveStats:
        stats = archive.get("stats", {})
        return cls(
            original_size=int(stats.get("original_size", 0)),
            compressed_size=int(stats.get("compressed_size", 0)),
            deduplicated_size=int(stats.get("deduplicated_size", 0)),
            nfiles=int(stats.get("nfiles", 0)),
            duration=archive.get("duration"),
            hostname=archive.get("hostname"),
            command_line=tuple(archive.get("command_line", []) or []),
        )


@dataclass(frozen=True, slots=True)
class RepositoryInfo:
    id: str
    location: str
    last_modified: datetime | None
    encryption_mode: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RepositoryInfo:
        repo = data.get("repository", {})
        return cls(
            id=repo.get("id", ""),
            location=repo.get("location", ""),
            last_modified=parse_borg_time(repo.get("last_modified")),
            encryption_mode=(data.get("encryption") or {}).get("mode"),
        )


@dataclass(frozen=True, slots=True)
class ArchiveItem:
    """One entry of ``borg list --json-lines`` (a file, directory, link, ...)."""

    path: str
    type: str  # '-' file, 'd' directory, 'l' symlink, 'h' hardlink, others: devices/fifos
    size: int
    mtime: datetime | None
    mode: str = ""
    user: str | None = None
    group: str | None = None
    uid: int | None = None
    gid: int | None = None
    healthy: bool = True
    linktarget: str | None = None

    @property
    def is_file(self) -> bool:
        return self.type == "-"

    @property
    def is_dir(self) -> bool:
        return self.type == "d"

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def parent(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ArchiveItem:
        return cls(
            path=data["path"],
            type=data.get("type", "-"),
            size=int(data.get("size") or 0),
            mtime=parse_borg_time(data.get("mtime") or data.get("isomtime")),
            mode=data.get("mode", ""),
            user=data.get("user"),
            group=data.get("group"),
            uid=data.get("uid"),
            gid=data.get("gid"),
            healthy=bool(data.get("healthy", True)),
            linktarget=data.get("linktarget") or data.get("source") or None,
        )


@dataclass(slots=True)
class ArchiveSummary:
    """Archive reference plus its (optional) statistics."""

    ref: ArchiveRef
    stats: ArchiveStats | None = None
    extra: dict[str, Any] = field(default_factory=dict)

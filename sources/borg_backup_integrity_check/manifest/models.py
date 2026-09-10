"""Manifest data model: stable, comparable metrics describing one backup generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MANIFEST_FORMAT = 1


@dataclass
class ArchiveMetrics:
    component: str
    name: str
    start: datetime
    original_size: int | None = None  # logical size (Borg "original size")
    compressed_size: int | None = None
    deduplicated_size: int | None = None
    nfiles: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "name": self.name,
            "start": self.start.isoformat(),
            "original_size": self.original_size,
            "compressed_size": self.compressed_size,
            "deduplicated_size": self.deduplicated_size,
            "nfiles": self.nfiles,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchiveMetrics:
        return cls(
            component=data["component"],
            name=data["name"],
            start=datetime.fromisoformat(data["start"]),
            original_size=data.get("original_size"),
            compressed_size=data.get("compressed_size"),
            deduplicated_size=data.get("deduplicated_size"),
            nfiles=data.get("nfiles"),
        )


@dataclass
class ComponentMetrics:
    id: str
    kind: str  # app | system_conf | system_data
    archive: str
    logical_size: int = 0  # sum of file sizes from the listing
    file_count: int = 0
    core_size: int = 0
    core_files: int = 0
    large_size: int = 0
    large_files: int = 0
    db_dump_size: int = 0
    large_roots: list[str] = field(default_factory=list)
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComponentMetrics:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class BackupManifest:
    generation_id: str
    backup_time: datetime
    generated_at: datetime
    archives: list[ArchiveMetrics] = field(default_factory=list)
    components: dict[str, ComponentMetrics] = field(default_factory=dict)
    repository_id: str | None = None
    yunohost_version: str | None = None
    stats_only: bool = False  # built from `borg info` only (no listing) - fewer metrics
    format: int = MANIFEST_FORMAT

    # ---- totals -------------------------------------------------------
    @property
    def total_logical(self) -> int | None:
        if self.stats_only:
            sizes = [a.original_size for a in self.archives if a.original_size is not None]
            return sum(sizes) if sizes else None
        return sum(c.logical_size for c in self.components.values())

    @property
    def total_files(self) -> int | None:
        if self.stats_only:
            counts = [a.nfiles for a in self.archives if a.nfiles is not None]
            return sum(counts) if counts else None
        return sum(c.file_count for c in self.components.values())

    @property
    def total_compressed(self) -> int | None:
        sizes = [a.compressed_size for a in self.archives if a.compressed_size is not None]
        return sum(sizes) if sizes else None

    @property
    def total_deduplicated(self) -> int | None:
        sizes = [a.deduplicated_size for a in self.archives if a.deduplicated_size is not None]
        return sum(sizes) if sizes else None

    def archive_for(self, component: str) -> ArchiveMetrics | None:
        for archive in self.archives:
            if archive.component == component:
                return archive
        return None

    # ---- (de)serialisation ------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "generation_id": self.generation_id,
            "backup_time": self.backup_time.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "repository_id": self.repository_id,
            "yunohost_version": self.yunohost_version,
            "stats_only": self.stats_only,
            "archives": [a.to_dict() for a in self.archives],
            "components": {k: v.to_dict() for k, v in self.components.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackupManifest:
        return cls(
            format=int(data.get("format", 1)),
            generation_id=data["generation_id"],
            backup_time=datetime.fromisoformat(data["backup_time"]),
            generated_at=datetime.fromisoformat(data["generated_at"]),
            repository_id=data.get("repository_id"),
            yunohost_version=data.get("yunohost_version"),
            stats_only=bool(data.get("stats_only", False)),
            archives=[ArchiveMetrics.from_dict(a) for a in data.get("archives", [])],
            components={
                k: ComponentMetrics.from_dict(v) for k, v in data.get("components", {}).items()
            },
        )

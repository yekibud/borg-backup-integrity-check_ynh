"""ManifestBuilder: turn a backup generation + discovered components into a manifest."""

from __future__ import annotations

from datetime import datetime

from ..borg.archives import BackupGeneration
from ..borg.client import BorgClient
from ..borg.models import ArchiveRef, ArchiveStats
from ..discovery.components import Component
from ..errors import BorgError
from ..logging_setup import get_logger
from .models import ArchiveMetrics, BackupManifest, ComponentMetrics

log = get_logger("manifest")


def archive_metrics(component: str, ref: ArchiveRef, stats: ArchiveStats | None) -> ArchiveMetrics:
    return ArchiveMetrics(
        component=component,
        name=ref.name,
        start=ref.start,
        original_size=stats.original_size if stats else None,
        compressed_size=stats.compressed_size if stats else None,
        deduplicated_size=stats.deduplicated_size if stats else None,
        nfiles=stats.nfiles if stats else None,
    )


def component_metrics(component: Component) -> ComponentMetrics:
    return ComponentMetrics(
        id=component.id,
        kind=component.kind,
        archive=component.archive.name,
        logical_size=component.logical_size,
        file_count=component.file_count,
        core_size=component.core_size,
        core_files=component.core_files,
        large_size=component.large_size,
        large_files=component.large_files,
        db_dump_size=component.db_dump_size,
        large_roots=[r.archive_path for r in component.large_roots],
        label=component.label,
    )


class ManifestBuilder:
    def __init__(self, client: BorgClient, repository_id: str | None = None) -> None:
        self.client = client
        self.repository_id = repository_id

    def fetch_stats(self, generation: BackupGeneration) -> dict[str, ArchiveStats | None]:
        stats: dict[str, ArchiveStats | None] = {}
        for component, ref in generation.archives.items():
            try:
                stats[component] = self.client.archive_stats(ref.name)
            except BorgError as exc:
                log.warning("no stats for %s: %s", ref.name, exc)
                stats[component] = None
        return stats

    def build(
        self,
        generation: BackupGeneration,
        components: list[Component],
        stats: dict[str, ArchiveStats | None] | None = None,
        yunohost_version: str | None = None,
    ) -> BackupManifest:
        stats = stats if stats is not None else self.fetch_stats(generation)
        manifest = BackupManifest(
            generation_id=generation.id,
            backup_time=generation.timestamp,
            generated_at=datetime.now(),
            repository_id=self.repository_id,
            yunohost_version=yunohost_version,
        )
        for comp, ref in sorted(generation.archives.items()):
            manifest.archives.append(archive_metrics(comp, ref, stats.get(comp)))
        for component in components:
            manifest.components[component.id] = component_metrics(component)
        return manifest

    def build_stats_only(self, generation: BackupGeneration) -> BackupManifest:
        """Cheap manifest (no listing pass) used as a baseline when no history exists yet."""
        stats = self.fetch_stats(generation)
        manifest = BackupManifest(
            generation_id=generation.id,
            backup_time=generation.timestamp,
            generated_at=datetime.now(),
            repository_id=self.repository_id,
            stats_only=True,
        )
        for comp, ref in sorted(generation.archives.items()):
            manifest.archives.append(archive_metrics(comp, ref, stats.get(comp)))
        return manifest

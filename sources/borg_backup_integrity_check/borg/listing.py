"""Streaming aggregation over archive item listings.

Nothing here keeps per-file records in memory: we aggregate size/count per
directory so that large-data discovery can look at the tree shape, and the
sampler later does a second streaming pass over the cached listing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from .models import ArchiveItem


@dataclass(slots=True)
class DirStats:
    size: int = 0
    files: int = 0
    newest_mtime: float = 0.0
    direct_files: int = 0


@dataclass
class DirectoryAggregates:
    """Recursive size/file-count per directory path ('' is the archive root)."""

    dirs: dict[str, DirStats] = field(default_factory=dict)
    total_size: int = 0
    total_files: int = 0
    unhealthy: list[str] = field(default_factory=list)
    tracked_files: dict[str, int] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        items: Iterable[ArchiveItem],
        track: Callable[[ArchiveItem], bool] | None = None,
    ) -> DirectoryAggregates:
        """Aggregate the listing; ``track`` may select individual files whose size is kept."""
        agg = cls()
        for item in items:
            if item.is_dir:
                agg.dirs.setdefault(item.path, DirStats())
                continue
            if not item.is_file:
                continue
            if not item.healthy:
                agg.unhealthy.append(item.path)
            if track is not None and track(item):
                agg.tracked_files[item.path] = item.size
            agg.total_size += item.size
            agg.total_files += 1
            mtime = item.mtime.timestamp() if item.mtime else 0.0
            parent = item.parent
            first = True
            path = parent
            while True:
                stats = agg.dirs.setdefault(path, DirStats())
                stats.size += item.size
                stats.files += 1
                if first:
                    stats.direct_files += 1
                    first = False
                if mtime > stats.newest_mtime:
                    stats.newest_mtime = mtime
                if path == "":
                    break
                path = path.rsplit("/", 1)[0] if "/" in path else ""
        return agg

    def get(self, path: str) -> DirStats:
        return self.dirs.get(path, DirStats())

    def children(self, path: str) -> Iterator[tuple[str, DirStats]]:
        prefix = f"{path}/" if path else ""
        depth = prefix.count("/")
        for candidate, stats in self.dirs.items():
            if candidate.startswith(prefix) and candidate != path and candidate.count("/") == depth:
                yield candidate, stats

    def subtree_size(self, prefix: str) -> tuple[int, int]:
        stats = self.get(prefix)
        return stats.size, stats.files


def iter_under(items: Iterable[ArchiveItem], prefixes: list[str]) -> Iterator[ArchiveItem]:
    """Yield items whose path lies under any of the given directory prefixes."""
    norm = [p.rstrip("/") for p in prefixes]
    for item in items:
        for prefix in norm:
            if item.path == prefix or item.path.startswith(prefix + "/"):
                yield item
                break

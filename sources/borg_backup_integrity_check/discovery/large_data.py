"""Generic discovery of large user-payload roots inside a backup component.

Evidence sources, in order of trust:

1. the app's ``data_dir`` setting (``settings.yml`` in the archive) mapped to its
   archive path through ``backup.csv``;
2. the ``resources.data_dir`` declaration of the app manifest;
3. system data parts (``data/mail``, ``data/home``, ``data/multimedia``) which
   YunoHost itself flags as big;
4. an optional sampling profile;
5. a size heuristic: a directory that dominates the component's size, contains
   many files and looks like user content rather than code or databases.

Everything not under a large root is "core" and restored normally, plus the small
files inside a large root that an app's restore script needs (see
``keep_small_files_in_large_roots``).
"""

from __future__ import annotations

import fnmatch
import heapq
import itertools
from collections.abc import Iterable
from dataclasses import dataclass

from ..borg.listing import DirectoryAggregates
from ..logging_setup import get_logger
from .components import Component, LargeRoot
from .profiles import ProfileRegistry, SamplingProfile

log = get_logger("discovery")

DB_DUMP_GLOBS = (
    "*.sql",
    "*.sql.gz",
    "*.dump",
    "*.pgdump",
    "db.sql",
    "dump.sql",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
)
CODE_DIR_HINTS = {
    "vendor",
    "node_modules",
    "lib",
    "bin",
    "venv",
    ".venv",
    "site-packages",
    "src",
    "include",
    "share",
}
CONFIG_ROOT_HINTS = ("etc/", "var/www/", "opt/", "usr/")


@dataclass
class HeuristicSettings:
    min_root_size: int = 200 * 1024 * 1024  # bytes
    min_root_files: int = 50
    dominance: float = 0.5  # subtree must hold at least this share of the component size
    descend_share: float = 0.8  # keep descending while a single child holds this share


class LargeDataDiscovery:
    def __init__(
        self,
        profiles: ProfileRegistry | None = None,
        heuristics: HeuristicSettings | None = None,
        enable_heuristic: bool = True,
    ) -> None:
        self.profiles = profiles or ProfileRegistry()
        self.heuristics = heuristics or HeuristicSettings()
        self.enable_heuristic = enable_heuristic

    # ------------------------------------------------------------------ API
    def discover(self, component: Component, aggregates: DirectoryAggregates) -> Component:
        """Populate ``component.large_roots`` / core sizes in place and return it."""
        roots: list[LargeRoot] = []
        if component.kind == "system_data":
            roots.extend(self._system_data_roots(component, aggregates))
        elif component.is_app:
            roots.extend(self._declared_app_roots(component, aggregates))
            profile = self.profiles.find(component.app.manifest_id if component.app else None)
            if profile:
                roots.extend(self._profile_roots(component, profile, aggregates))
            if self.enable_heuristic and not roots:
                heuristic = self._heuristic_root(component, aggregates)
                if heuristic:
                    roots.append(heuristic)
        component.large_roots = _dedupe_roots(roots)
        self._compute_sizes(component, aggregates)
        return component

    # ------------------------------------------------------------- sources
    def _system_data_roots(self, component: Component, agg: DirectoryAggregates) -> list[LargeRoot]:
        stats = agg.get(component.root)
        live = {
            "data/mail": "/var/mail",
            "data/home": "/home",
            "data/multimedia": "/home/yunohost.multimedia",
        }
        return [
            LargeRoot(
                archive_path=component.root,
                live_path=live.get(component.root),
                origin="system_data_part",
                size=stats.size,
                files=stats.files,
                label=component.label,
            )
        ]

    def _declared_app_roots(
        self, component: Component, agg: DirectoryAggregates
    ) -> list[LargeRoot]:
        roots: list[LargeRoot] = []
        app = component.app
        if app is None:
            return roots
        candidates: list[tuple[str, str]] = []
        if app.data_dir:
            candidates.append((app.data_dir, "data_dir_setting"))
        if app.manifest_data_dir and app.manifest_data_dir != app.data_dir:
            candidates.append((app.manifest_data_dir, "manifest_data_dir"))
        for live_path, origin in candidates:
            archive_path = self._archive_path_for_live(component, live_path, agg)
            if archive_path is None:
                component.notes.append(f"{origin}: {live_path} is not present in the archive")
                continue
            stats = agg.get(archive_path)
            roots.append(
                LargeRoot(
                    archive_path=archive_path,
                    live_path=live_path,
                    origin=origin,
                    size=stats.size,
                    files=stats.files,
                )
            )
        return roots

    def _profile_roots(
        self, component: Component, profile: SamplingProfile, agg: DirectoryAggregates
    ) -> list[LargeRoot]:
        roots: list[LargeRoot] = []
        app = component.app
        for template in profile.roots:
            live_path = template
            if app is not None:
                live_path = (
                    template.replace("__DATA_DIR__", app.data_dir or "")
                    .replace("__INSTALL_DIR__", app.install_dir or "")
                    .replace("__APP__", app.app)
                )
            if not live_path or "__" in live_path:
                continue
            archive_path = self._archive_path_for_live(component, live_path, agg)
            if archive_path is None:
                continue
            stats = agg.get(archive_path)
            roots.append(
                LargeRoot(
                    archive_path=archive_path,
                    live_path=live_path,
                    origin="profile",
                    size=stats.size,
                    files=stats.files,
                    label=profile.name,
                )
            )
        return roots

    def _heuristic_root(self, component: Component, agg: DirectoryAggregates) -> LargeRoot | None:
        backup_root = f"{component.root}/backup"
        total = agg.get(backup_root)
        if (
            total.size < self.heuristics.min_root_size
            or total.files < self.heuristics.min_root_files
        ):
            return None
        node = backup_root
        # Descend while a single child dominates; stop at the widest "content" directory.
        for _ in range(12):
            children = sorted(agg.children(node), key=lambda kv: kv[1].size, reverse=True)
            if not children:
                break
            best_path, best = children[0]
            if best.size < total.size * self.heuristics.dominance:
                break
            name = best_path.rsplit("/", 1)[-1]
            if name in CODE_DIR_HINTS or _looks_like_db_dump(best_path):
                break
            node = best_path
            if best.size < self.heuristics.descend_share * agg.get(node).size:
                break
        if node == backup_root:
            return None
        stats = agg.get(node)
        rel = node[len(backup_root) + 1 :]
        if (
            any(rel.startswith(h) for h in CONFIG_ROOT_HINTS)
            and stats.files < self.heuristics.min_root_files * 4
        ):
            return None
        if (
            stats.size < self.heuristics.min_root_size
            or stats.files < self.heuristics.min_root_files
        ):
            return None
        live_path = component.layout.source_for_dest(node)
        component.notes.append(
            f"size heuristic: {rel} holds {stats.files} files ({stats.size} bytes)"
        )
        return LargeRoot(
            archive_path=node,
            live_path=live_path,
            origin="size_heuristic",
            size=stats.size,
            files=stats.files,
        )

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _archive_path_for_live(
        component: Component, live_path: str, agg: DirectoryAggregates
    ) -> str | None:
        """Map a live path (``/home/yunohost.app/x``) to its archive path using backup.csv."""
        layout = component.layout
        live_path = live_path.rstrip("/")
        exact = layout.dest_for_source(live_path)
        if exact and exact in agg.dirs:
            return exact
        # The data dir may be nested under a broader row (e.g. backup of /home/yunohost.app).
        for row in layout.rows_for_app(component.id):
            source = row.source.rstrip("/")
            if live_path.startswith(source + "/"):
                nested = row.dest.rstrip("/") + live_path[len(source) :]
                if nested in agg.dirs:
                    return nested
        # Fallback: the conventional destination apps/<app>/backup/<live path>.
        guess = f"{component.root}/backup/{live_path.strip('/')}"
        return guess if guess in agg.dirs else None

    def _compute_sizes(self, component: Component, agg: DirectoryAggregates) -> None:
        total = agg.get(component.root)
        for root in component.large_roots:
            stats = agg.get(root.archive_path)
            root.size, root.files = stats.size, stats.files
        large = sum(r.size for r in component.large_roots)
        large_files = sum(r.files for r in component.large_roots)
        component.core_size = max(total.size - large, 0)
        component.core_files = max(total.files - large_files, 0)
        dump_prefix = f"{component.root}/backup/"
        dumps = {
            path: size
            for path, size in agg.tracked_files.items()
            if path.startswith(dump_prefix)
            and path[len(dump_prefix) :].count("/") <= 1
            and not component.is_large(path)
        }
        component.db_dump_paths = sorted(dumps)
        component.db_dump_size = sum(dumps.values())


def _looks_like_db_dump(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(name, g) for g in DB_DUMP_GLOBS)


def _dedupe_roots(roots: list[LargeRoot]) -> list[LargeRoot]:
    """Drop roots nested inside other roots and exact duplicates (first origin wins)."""
    result: list[LargeRoot] = []
    for root in sorted(roots, key=lambda r: len(r.archive_path)):
        if any(existing.contains(root.archive_path) for existing in result):
            continue
        result.append(root)
    return result


def is_db_dump_item(item) -> bool:
    """Predicate for ``DirectoryAggregates.build(track=...)``: database dump files."""
    return item.is_file and _looks_like_db_dump(item.path)


# An app's restore script reads scripts, dumps and configuration that live *inside* its data
# directory (immich chowns backups/restore_immich_db_backup.sh there). Excluding the whole root
# makes those restores fail, so the app's own plumbing comes along - never user payload, which
# always sits deeper, under per-user or per-object directories.
KEEP_FILE_MAX_BYTES = 1 << 20
KEEP_SHALLOW_DEPTH = 1  # the root itself and one directory down
KEEP_PLUMBING_DEPTH = 3  # deeper only when the name says plumbing, not payload
KEEP_FILES_PER_ROOT = 2000
KEEP_BYTES_PER_ROOT = 256 << 20
PLUMBING_SUFFIXES = (
    ".sh",
    ".sql",
    ".conf",
    ".cfg",
    ".ini",
    ".env",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".service",
)


def _is_plumbing(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name.endswith(PLUMBING_SUFFIXES)


def keep_small_files_in_large_roots(component: Component, items: Iterable) -> int:
    """Pick the small plumbing files of each large root; returns how many were kept in total.

    Bounded in memory (a per-root heap of the best candidates) because a large root can hold
    hundreds of thousands of small files - thumbnails, chunks - that must not all be kept.
    """
    roots = component.large_roots
    if not roots:
        return 0
    heaps: dict[str, list] = {root.archive_path: [] for root in roots}
    tie = itertools.count()
    for item in items:
        if not item.is_file or item.size > KEEP_FILE_MAX_BYTES:
            continue
        for root_path, heap in heaps.items():
            if not item.path.startswith(root_path + "/"):
                continue
            depth = item.path[len(root_path) + 1 :].count("/")
            if depth <= KEEP_SHALLOW_DEPTH or (
                depth <= KEEP_PLUMBING_DEPTH and _is_plumbing(item.path)
            ):
                # The heap's root is the worst candidate (deepest, then largest), so it is the
                # one to drop once the budget is full.
                key = (-depth, -item.size)
                entry = (key, next(tie), item.path, item.size)
                if len(heap) < KEEP_FILES_PER_ROOT:
                    heapq.heappush(heap, entry)
                elif key > heap[0][0]:
                    heapq.heapreplace(heap, entry)
            break
    kept = 0
    for root in roots:
        chosen: list[str] = []
        total = 0
        shallowest_first = sorted(
            heaps[root.archive_path], key=lambda e: (-e[0][0], -e[0][1], e[2])
        )
        for _key, _tie, path, size in shallowest_first:
            if total + size > KEEP_BYTES_PER_ROOT:
                continue
            chosen.append(path)
            total += size
        root.keep_files = sorted(chosen)
        root.keep_bytes = total
        kept += len(chosen)
    return kept

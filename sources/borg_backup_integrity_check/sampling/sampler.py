"""GenericSampler: pick the newest N meaningful objects under each large root.

The sampler only looks at Borg listing metadata (path, size, mtime), never at
file contents, so choosing the sample costs no payload transfer. Selection is
content-generic: it skips conventional cache/index/temporary locations and
zero-byte files, recognises Git repositories as single objects and can spread
the sample across top-level groups (e.g. one maildir or one user per group).
"""

from __future__ import annotations

import fnmatch
import heapq
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from ..borg.models import ArchiveItem
from ..discovery.components import Component, LargeRoot

# Directory names that conventionally hold generated, derivative or transient data.
DEFAULT_EXCLUDE_DIRS: tuple[str, ...] = (
    "cache",
    ".cache",
    "caches",
    "tmp",
    ".tmp",
    "temp",
    "thumbnails",
    ".thumbnails",
    "thumbs",
    "thumb",
    "preview",
    "previews",
    "appdata_*",
    "files_versions",
    "files_trashbin",
    "versions",
    "trash",
    ".Trash*",
    "index",
    "indexes",
    "__pycache__",
    "node_modules",
    "encoded-video",
    "logs",
    "log",
    "sessions",
    "lost+found",
    ".sync",
    ".git",
    "uploads",
    "upload",
    "locks",
    "snapshots",
    "files_external",
)
# File name patterns for index/lock/partial/OS-cruft files.
DEFAULT_EXCLUDE_FILES: tuple[str, ...] = (
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    "*.part",
    "*.tmp",
    "*.lock",
    "*.lck",
    "*.swp",
    "*~",
    "dovecot.index*",
    "dovecot-uidlist",
    "dovecot-uidvalidity*",
    "dovecot.list.index*",
    "dovecot-keywords",
    "dovecot.mailbox.log",
    "maildirsize",
    "subscriptions",
    ".nobackup",
    "*.pyc",
    "*.log",
    ".htaccess",
    "*.sqlite-journal",
    "*.db-wal",
    "*.db-shm",
    ".ocdata",
    "core",
    "*.pid",
)
# Maildir "tmp" holds messages being delivered: never meaningful.
MAILDIR_TMP_SEGMENT = "tmp"


@dataclass
class SamplingRules:
    sample_size: int = 20
    exclude_dirs: tuple[str, ...] = DEFAULT_EXCLUDE_DIRS
    exclude_files: tuple[str, ...] = DEFAULT_EXCLUDE_FILES
    include_globs: tuple[str, ...] = ()
    min_size: int = 1
    max_size: int | None = 2 * 1024**3  # skip enormous single objects (e.g. disk images)
    spread_groups: bool = True
    newer_than: datetime | None = None

    def with_profile_exclusions(
        self, dirs: Iterable[str], files: Iterable[str], includes: Iterable[str]
    ) -> SamplingRules:
        return SamplingRules(
            sample_size=self.sample_size,
            exclude_dirs=tuple(self.exclude_dirs) + tuple(dirs),
            exclude_files=tuple(self.exclude_files) + tuple(files),
            include_globs=tuple(self.include_globs) + tuple(includes),
            min_size=self.min_size,
            max_size=self.max_size,
            spread_groups=self.spread_groups,
            newer_than=self.newer_than,
        )


@dataclass(frozen=True)
class SampledObject:
    """An object chosen for retrieval: a file or a whole Git repository directory."""

    archive_path: str
    live_path: str | None
    root: str  # archive path of the large root it belongs to
    relative_path: str  # path relative to the large root
    size: int
    mtime: datetime | None
    kind: str = "file"  # file | git_repo
    user: str | None = None
    group: str | None = None
    mode: str = ""

    @property
    def sort_key(self) -> float:
        return self.mtime.timestamp() if self.mtime else 0.0

    def to_dict(self) -> dict:
        return {
            "archive_path": self.archive_path,
            "live_path": self.live_path,
            "root": self.root,
            "relative_path": self.relative_path,
            "size": self.size,
            "mtime": self.mtime.isoformat() if self.mtime else None,
            "kind": self.kind,
            "user": self.user,
            "group": self.group,
            "mode": self.mode,
        }


@dataclass
class RootSample:
    root: LargeRoot
    objects: list[SampledObject] = field(default_factory=list)
    candidates_seen: int = 0
    skipped_excluded: int = 0
    groups_seen: int = 0


class GenericSampler:
    def __init__(self, rules: SamplingRules | None = None) -> None:
        self.rules = rules or SamplingRules()

    def select(self, component: Component, items: Iterable[ArchiveItem]) -> list[RootSample]:
        """Stream ``items`` (the component's archive listing) and select per-root samples."""
        rules = self.rules
        samples = {root.archive_path: RootSample(root=root) for root in component.large_roots}
        # Per root: group -> heap of (sort_key, seq, candidate) bounded to sample_size.
        heaps: dict[str, dict[str, list[tuple[float, int, SampledObject]]]] = {
            k: {} for k in samples
        }
        seen_git_roots: set[str] = set()
        seq = 0
        n = rules.sample_size
        newer_than = rules.newer_than.timestamp() if rules.newer_than else None

        for item in items:
            root = self._root_for(component, item.path)
            if root is None:
                continue
            sample = samples[root.archive_path]
            rel = item.path[len(root.archive_path) + 1 :] if item.path != root.archive_path else ""
            if not rel:
                continue
            candidate = self._candidate(item, root, rel, seen_git_roots)
            if candidate is None:
                continue
            if candidate.kind == "file" and not self._passes(rel, item, rules):
                sample.skipped_excluded += 1
                continue
            sample.candidates_seen += 1
            key = candidate.sort_key
            if newer_than is not None and key < newer_than:
                continue
            group = rel.split("/", 1)[0] if rules.spread_groups and "/" in rel else "_"
            heap = heaps[root.archive_path].setdefault(group, [])
            seq += 1
            entry = (key, seq, candidate)
            if len(heap) < n:
                heapq.heappush(heap, entry)
            elif entry > heap[0]:
                heapq.heapreplace(heap, entry)

        for root_path, sample in samples.items():
            groups = heaps[root_path]
            sample.groups_seen = len(groups)
            sample.objects = self._merge(groups, n, rules.spread_groups)
        return list(samples.values())

    # ------------------------------------------------------------ internals
    @staticmethod
    def _root_for(component: Component, path: str) -> LargeRoot | None:
        for root in component.large_roots:
            if root.contains(path):
                return root
        return None

    @staticmethod
    def _candidate(
        item: ArchiveItem, root: LargeRoot, rel: str, seen_git: set[str]
    ) -> SampledObject | None:
        parts = rel.split("/")
        if ".git" in parts:
            idx = parts.index(".git")
            repo_rel = "/".join(parts[:idx])
            if item.name == "HEAD" and idx == len(parts) - 2 and repo_rel not in seen_git:
                seen_git.add(repo_rel)
                repo_archive = f"{root.archive_path}/{repo_rel}" if repo_rel else root.archive_path
                live = f"{root.live_path.rstrip('/')}/{repo_rel}" if root.live_path else None
                return SampledObject(
                    archive_path=repo_archive,
                    live_path=live.rstrip("/") if live else None,
                    root=root.archive_path,
                    relative_path=repo_rel,
                    size=item.size,
                    mtime=item.mtime,
                    kind="git_repo",
                    user=item.user,
                    group=item.group,
                    mode=item.mode,
                )
            return None
        if not item.is_file:
            return None
        live = f"{root.live_path.rstrip('/')}/{rel}" if root.live_path else None
        return SampledObject(
            archive_path=item.path,
            live_path=live,
            root=root.archive_path,
            relative_path=rel,
            size=item.size,
            mtime=item.mtime,
            user=item.user,
            group=item.group,
            mode=item.mode,
        )

    @staticmethod
    def _passes(rel: str, item: ArchiveItem, rules: SamplingRules) -> bool:
        if item.size < rules.min_size:
            return False
        if rules.max_size is not None and item.size > rules.max_size:
            return False
        if not item.healthy:
            return False
        segments = rel.split("/")
        dirs, name = segments[:-1], segments[-1]
        if rules.include_globs and not any(
            fnmatch.fnmatchcase(rel, g) for g in rules.include_globs
        ):
            return False
        for directory in dirs:
            for pattern in rules.exclude_dirs:
                if fnmatch.fnmatchcase(directory, pattern):
                    return False
        # Maildir tmp/ holds in-flight deliveries: transient, never meaningful.
        if dirs and dirs[-1] == MAILDIR_TMP_SEGMENT:
            return False
        if name.startswith(".") and name not in {".gitignore"}:
            return False
        return all(not fnmatch.fnmatchcase(name, pattern) for pattern in rules.exclude_files)

    @staticmethod
    def _merge(
        groups: dict[str, list[tuple[float, int, SampledObject]]], n: int, spread: bool
    ) -> list[SampledObject]:
        if not groups:
            return []
        if not spread or len(groups) == 1:
            merged = [entry for heap in groups.values() for entry in heap]
            merged.sort(reverse=True)
            return [e[2] for e in merged[:n]]
        # Round-robin over groups ordered by their newest object, newest first within a group.
        ordered = {g: sorted(h, reverse=True) for g, h in groups.items()}
        group_order = sorted(
            ordered, key=lambda g: ordered[g][0][0] if ordered[g] else 0, reverse=True
        )
        result: list[SampledObject] = []
        cursors = dict.fromkeys(group_order, 0)
        while len(result) < n:
            progressed = False
            for g in group_order:
                lst = ordered[g]
                if cursors[g] < len(lst):
                    result.append(lst[cursors[g]][2])
                    cursors[g] += 1
                    progressed = True
                    if len(result) >= n:
                        break
            if not progressed:
                break
        return result

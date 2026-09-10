"""Archive naming schemes and backup-generation selection.

borg_ynh creates one archive per component and run::

    auto_conf-2026-09-10T02:01:03      (system configuration parts)
    auto_data-2026-09-10T02:03:10      (system data parts: mail, homes, multimedia)
    auto_nextcloud-2026-09-10T02:05:44 (one per app)

A *generation* is the set of newest archives (one per component) that were
created within a configurable window of each other. Other tools may put a
complete YunoHost backup into a single archive; the ``single`` scheme covers
that case (component id ``all``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import ArchiveRef

BORG_YNH_PATTERN = r"^(?P<component>.+?)-(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})$"
SINGLE_ARCHIVE_COMPONENT = "all"

SYSTEM_CONF_COMPONENT = "auto_conf"
SYSTEM_DATA_COMPONENT = "auto_data"
APP_COMPONENT_PREFIX = "auto_"


@dataclass(frozen=True)
class ArchiveNamingScheme:
    """Maps archive names to (component, timestamp)."""

    name: str = "borg_ynh"
    pattern: str = BORG_YNH_PATTERN

    def parse(self, archive_name: str) -> tuple[str, str | None] | None:
        if self.name == "single":
            return (SINGLE_ARCHIVE_COMPONENT, None)
        match = re.match(self.pattern, archive_name)
        if not match:
            return None
        groups = match.groupdict()
        return (groups.get("component") or archive_name, groups.get("timestamp"))

    @classmethod
    def from_settings(cls, scheme: str, custom_pattern: str | None = None) -> ArchiveNamingScheme:
        if scheme == "single":
            return cls(name="single", pattern="")
        if scheme == "custom" and custom_pattern:
            re.compile(custom_pattern)
            return cls(name="custom", pattern=custom_pattern)
        return cls()


def component_kind(component: str) -> str:
    """``system_conf``, ``system_data``, ``app`` or ``all``."""
    if component == SINGLE_ARCHIVE_COMPONENT:
        return "all"
    if component == SYSTEM_CONF_COMPONENT:
        return "system_conf"
    if component == SYSTEM_DATA_COMPONENT:
        return "system_data"
    return "app"


def component_app_id(component: str) -> str | None:
    """``auto_nextcloud`` -> ``nextcloud``; system/single components -> ``None``."""
    if component_kind(component) != "app":
        return None
    if component.startswith(APP_COMPONENT_PREFIX):
        return component[len(APP_COMPONENT_PREFIX) :]
    return component


@dataclass
class BackupGeneration:
    """Newest archive per component, plus components whose newest archive is stale."""

    timestamp: datetime
    archives: dict[str, ArchiveRef] = field(default_factory=dict)
    stale: dict[str, ArchiveRef] = field(default_factory=dict)

    @property
    def components(self) -> list[str]:
        return sorted(self.archives)

    @property
    def id(self) -> str:
        return self.timestamp.strftime("%Y%m%dT%H%M%S")

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or datetime.now()) - self.timestamp


@dataclass
class ArchiveCatalog:
    """All parseable archives grouped by component (sorted oldest -> newest)."""

    scheme: ArchiveNamingScheme
    by_component: dict[str, list[ArchiveRef]] = field(default_factory=dict)
    unparsed: list[ArchiveRef] = field(default_factory=list)

    @classmethod
    def build(cls, scheme: ArchiveNamingScheme, archives: list[ArchiveRef]) -> ArchiveCatalog:
        catalog = cls(scheme=scheme)
        for ref in sorted(archives, key=lambda a: a.start):
            parsed = scheme.parse(ref.name)
            if parsed is None:
                catalog.unparsed.append(ref)
                continue
            catalog.by_component.setdefault(parsed[0], []).append(ref)
        return catalog

    def generations(self, window: timedelta = timedelta(hours=12)) -> list[BackupGeneration]:
        """Cluster archives into generations, newest first.

        Each cluster starts from the newest not-yet-assigned archive and absorbs the
        newest archive of every other component that lies within ``window`` before it.
        """
        remaining: dict[str, list[ArchiveRef]] = {
            comp: list(refs) for comp, refs in self.by_component.items()
        }
        generations: list[BackupGeneration] = []
        while any(remaining.values()):
            newest = max((refs[-1] for refs in remaining.values() if refs), key=lambda a: a.start)
            gen = BackupGeneration(timestamp=newest.start)
            for comp, refs in remaining.items():
                if not refs:
                    continue
                candidate = refs[-1]
                if newest.start - candidate.start <= window:
                    gen.archives[comp] = candidate
                    refs.pop()
            generations.append(gen)
        return generations

    def latest_generation(
        self,
        window: timedelta = timedelta(hours=12),
        expected_components: list[str] | None = None,
    ) -> BackupGeneration | None:
        generations = self.generations(window)
        if not generations:
            return None
        latest = generations[0]
        # Components known from a previous run but absent from the newest generation are
        # reported as stale with their last known archive (or missing entirely).
        for comp in expected_components or []:
            if comp not in latest.archives and self.by_component.get(comp):
                latest.stale[comp] = self.by_component[comp][-1]
        for comp, refs in self.by_component.items():
            if comp not in latest.archives and comp not in latest.stale and refs:
                latest.stale[comp] = refs[-1]
        return latest

    def previous_generation(
        self, current: BackupGeneration, window: timedelta = timedelta(hours=12)
    ) -> BackupGeneration | None:
        for gen in self.generations(window):
            if gen.timestamp < current.timestamp:
                return gen
        return None

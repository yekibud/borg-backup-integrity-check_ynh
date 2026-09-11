"""Component model: what one integrity run restores and samples.

A component is either an application (``apps/<app>`` in its archive) or a
system part (``conf_*`` / ``data_*``). Each component knows its *core* subtree
(always restored) and its *large roots* (only sampled by default).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..borg.archives import component_app_id, component_kind
from ..borg.layout import AppArchiveEntry, BackupLayout
from ..borg.models import ArchiveRef


@dataclass
class LargeRoot:
    """A directory subtree inside the archive considered large user payload."""

    archive_path: str  # e.g. apps/nextcloud/backup/home/yunohost.app/nextcloud
    live_path: str | None  # e.g. /home/yunohost.app/nextcloud
    origin: (
        str  # data_dir_setting | manifest_data_dir | system_data_part | size_heuristic | profile
    )
    size: int = 0
    files: int = 0
    label: str | None = None

    def contains(self, archive_path: str) -> bool:
        return archive_path == self.archive_path or archive_path.startswith(self.archive_path + "/")


@dataclass
class Component:
    id: str  # e.g. "nextcloud", "data_mail", "conf_ldap"
    kind: str  # app | system_conf | system_data
    archive: ArchiveRef
    archive_component: (
        str  # naming-scheme component the archive belongs to (auto_nextcloud, auto_data, ...)
    )
    root: str  # archive subtree: apps/<app> | data/mail | conf/ldap ...
    layout: BackupLayout
    app: AppArchiveEntry | None = None
    large_roots: list[LargeRoot] = field(default_factory=list)
    core_size: int = 0
    core_files: int = 0
    db_dump_size: int = 0
    db_dump_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # False when the archive's full file listing was skipped (component not selected for sampling):
    # the manifest then uses archive-level `borg info` stats for its size/object count.
    listed: bool = True

    @property
    def is_app(self) -> bool:
        return self.kind == "app"

    @property
    def large_size(self) -> int:
        return sum(r.size for r in self.large_roots)

    @property
    def large_files(self) -> int:
        return sum(r.files for r in self.large_roots)

    @property
    def logical_size(self) -> int:
        return self.core_size + self.large_size

    @property
    def file_count(self) -> int:
        return self.core_files + self.large_files

    @property
    def label(self) -> str:
        if self.is_app:
            return self.id
        return {
            "data_mail": "Mail data",
            "data_home": "User home directories",
            "data_multimedia": "Multimedia data",
            "conf_ldap": "Users & groups (LDAP)",
            "conf_ynh_settings": "YunoHost settings",
            "conf_ynh_certs": "Certificates",
            "conf_manually_modified_files": "Manually modified files",
        }.get(self.id, self.id)

    def is_large(self, archive_path: str) -> bool:
        return any(root.contains(archive_path) for root in self.large_roots)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "archive": self.archive.name,
            "root": self.root,
            "core_size": self.core_size,
            "core_files": self.core_files,
            "db_dump_size": self.db_dump_size,
            "large_roots": [
                {
                    "archive_path": r.archive_path,
                    "live_path": r.live_path,
                    "origin": r.origin,
                    "size": r.size,
                    "files": r.files,
                }
                for r in self.large_roots
            ],
            "notes": list(self.notes),
        }


def components_from_layout(
    archive: ArchiveRef, archive_component: str, layout: BackupLayout
) -> list[Component]:
    """Instantiate components for every app and system part contained in an archive."""
    components: list[Component] = []
    kind = component_kind(archive_component)
    app_hint = component_app_id(archive_component)

    app_names = layout.app_names()
    if app_hint and app_hint in app_names:
        app_names = [app_hint]
    for app in app_names:
        components.append(
            Component(
                id=app,
                kind="app",
                archive=archive,
                archive_component=archive_component,
                root=f"apps/{app}",
                layout=layout,
                app=layout.apps.get(app) or AppArchiveEntry(app=app),
            )
        )
    if kind in {"system_conf", "system_data", "all"}:
        for part in layout.system_parts:
            part_kind = "system_data" if part.startswith("data_") else "system_conf"
            components.append(
                Component(
                    id=part,
                    kind=part_kind,
                    archive=archive,
                    archive_component=archive_component,
                    root=part.replace("_", "/", 1),
                    layout=layout,
                )
            )
    return components

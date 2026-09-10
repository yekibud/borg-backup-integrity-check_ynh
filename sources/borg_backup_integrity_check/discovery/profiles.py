"""Optional declarative sampling profiles (small TOML files, one per upstream app).

Profiles *augment* generic discovery; they are never required. A profile may:

* declare additional large roots relative to the app's data or install dir,
* add directory/file exclusion patterns for sampling,
* describe how to reach a sampled object through the restored application
  (``verify.http_path`` template, used by the health checker when possible),
* give the component a human label/kind (``file``, ``media``, ``mail``, ``repo``).

Example ``profiles/nextcloud.toml``::

    match_ids = ["nextcloud"]
    kind = "file"
    [large_data]
    roots = ["__DATA_DIR__"]
    exclude_dirs = ["appdata_*", "files_versions", "files_trashbin", "uploads", "cache"]
    include_globs = ["*/files/**"]
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("profiles")

BUILTIN_PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"


@dataclass
class SamplingProfile:
    name: str
    match_ids: list[str] = field(default_factory=list)
    kind: str | None = None
    roots: list[str] = field(default_factory=list)
    exclude_dirs: list[str] = field(default_factory=list)
    exclude_files: list[str] = field(default_factory=list)
    include_globs: list[str] = field(default_factory=list)
    http_object_path: str | None = None
    services: list[str] = field(default_factory=list)
    http_ok_codes: list[int] = field(default_factory=list)

    def matches(self, app_manifest_id: str) -> bool:
        return any(fnmatch.fnmatchcase(app_manifest_id, pattern) for pattern in self.match_ids)

    @classmethod
    def from_toml(cls, name: str, text: str) -> SamplingProfile:
        data = tomllib.loads(text)
        large = data.get("large_data", {}) or {}
        verify = data.get("verify", {}) or {}
        return cls(
            name=name,
            match_ids=list(data.get("match_ids", []) or []),
            kind=data.get("kind"),
            roots=list(large.get("roots", []) or []),
            exclude_dirs=list(large.get("exclude_dirs", []) or []),
            exclude_files=list(large.get("exclude_files", []) or []),
            include_globs=list(large.get("include_globs", []) or []),
            http_object_path=verify.get("http_object_path"),
            services=list(verify.get("services", []) or []),
            http_ok_codes=[int(c) for c in verify.get("http_ok_codes", []) or []],
        )


class ProfileRegistry:
    def __init__(self, directories: list[Path] | None = None) -> None:
        self.profiles: list[SamplingProfile] = []
        for directory in directories or [BUILTIN_PROFILE_DIR]:
            self.load_dir(directory)

    def load_dir(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.toml")):
            try:
                self.profiles.append(
                    SamplingProfile.from_toml(path.stem, path.read_text(encoding="utf-8"))
                )
            except (OSError, tomllib.TOMLDecodeError) as exc:
                log.warning("ignoring invalid profile %s: %s", path, exc)

    def find(self, app_manifest_id: str | None) -> SamplingProfile | None:
        if not app_manifest_id:
            return None
        for profile in self.profiles:
            if profile.matches(app_manifest_id):
                return profile
        return None

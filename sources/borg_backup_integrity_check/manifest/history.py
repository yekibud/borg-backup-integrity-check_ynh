"""HistoryStore: small JSON manifests persisted on the production server for comparisons."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from ..logging_setup import get_logger
from .models import BackupManifest

log = get_logger("history")


class HistoryStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, manifest: BackupManifest) -> Path:
        return self.directory / f"{manifest.generation_id}.json"

    def save(self, manifest: BackupManifest) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(manifest)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest.to_dict(), fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        return path

    def load_all(self) -> list[BackupManifest]:
        manifests: list[BackupManifest] = []
        if not self.directory.is_dir():
            return manifests
        for path in sorted(self.directory.glob("*.json")):
            try:
                with open(path, encoding="utf-8") as fh:
                    manifests.append(BackupManifest.from_dict(json.load(fh)))
            except (OSError, ValueError, KeyError) as exc:
                log.warning("ignoring unreadable manifest %s: %s", path, exc)
        manifests.sort(key=lambda m: m.backup_time)
        return manifests

    def latest(self) -> BackupManifest | None:
        manifests = self.load_all()
        return manifests[-1] if manifests else None

    def previous_to(self, manifest: BackupManifest) -> BackupManifest | None:
        """The most recent stored manifest for an *earlier* backup generation."""
        candidates = [m for m in self.load_all() if m.backup_time < manifest.backup_time]
        return candidates[-1] if candidates else None

    def window(self, manifest: BackupManifest, days: int) -> list[BackupManifest]:
        start = manifest.backup_time - timedelta(days=days)
        return [m for m in self.load_all() if start <= m.backup_time < manifest.backup_time]

    def known_components(self) -> list[str]:
        latest = self.latest()
        if latest is None:
            return []
        return sorted({a.component for a in latest.archives})

    def prune(self, retention_days: int, now: datetime | None = None) -> int:
        now = now or datetime.now()
        cutoff = now - timedelta(days=retention_days)
        removed = 0
        for manifest in self.load_all():
            if manifest.backup_time < cutoff:
                try:
                    self._path(manifest).unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

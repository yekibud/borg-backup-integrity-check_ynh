"""Persisted run state so interrupted runs can be cleaned up later."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("state")

PHASES = (
    "created",
    "inspecting",
    "provisioning",
    "bootstrapping",
    "restoring",
    "sampling",
    "verifying",
    "reporting",
    "cleaning",
    "retained",
    "finished",
)


@dataclass
class RunState:
    run_id: str
    mode: str
    provider: str
    started_at: datetime
    phase: str = "created"
    status: str = "running"  # running | retained | finished | failed | interrupted
    owner: str | None = None
    region: str | None = None
    server_id: str | None = None
    server_name: str | None = None
    volume_ids: list[str] = field(default_factory=list)
    ssh_key_id: str | None = None
    host_address: str | None = None
    host_ssh_port: int | None = None
    retained_until: datetime | None = None
    finished_at: datetime | None = None
    cleanup_status: str = "pending"  # pending | done | failed | not_needed
    cleanup_error: str | None = None
    cleanup_attempts: int = 0
    report_path: str | None = None
    overall: str | None = None
    error: str | None = None
    failed_phase: str | None = None  # phase the run died in (``phase`` moves on to cleaning)
    notes: list[str] = field(default_factory=list)

    @property
    def has_cloud_resources(self) -> bool:
        return bool(self.server_id or self.volume_ids)

    @property
    def needs_cleanup(self) -> bool:
        return self.has_cloud_resources and self.cleanup_status not in ("done", "not_needed")

    def to_dict(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        for key in ("started_at", "retained_until", "finished_at"):
            value = data.get(key)
            data[key] = value.isoformat() if isinstance(value, datetime) else value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        kwargs = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        for key in ("started_at", "retained_until", "finished_at"):
            if kwargs.get(key):
                kwargs[key] = datetime.fromisoformat(kwargs[key])
        return cls(**kwargs)


class RunStateStore:
    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def save(self, state: RunState) -> None:
        directory = self.run_dir(state.run_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = directory / "state.json.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=1, sort_keys=True, default=str)
        os.chmod(tmp, 0o600)
        tmp.replace(directory / "state.json")

    def load(self, run_id: str) -> RunState | None:
        path = self.run_dir(run_id) / "state.json"
        if not path.is_file():
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return RunState.from_dict(json.load(fh))
        except (OSError, ValueError, TypeError) as exc:
            log.warning("unreadable run state %s: %s", path, exc)
            return None

    def list(self) -> list[RunState]:
        if not self.runs_dir.is_dir():
            return []
        states = []
        for entry in sorted(self.runs_dir.iterdir()):
            state = self.load(entry.name)
            if state:
                states.append(state)
        return states

    def latest(self) -> RunState | None:
        states = self.list()
        return states[-1] if states else None

    def unfinished(self) -> list[RunState]:
        return [s for s in self.list() if s.needs_cleanup or s.status in ("running", "retained")]

    def prune(self, keep: int) -> int:
        """Delete the oldest run directories beyond ``keep`` (never ones needing cleanup)."""
        import shutil

        states = [
            s
            for s in self.list()
            if not s.needs_cleanup and s.status not in ("running", "retained")
        ]
        removed = 0
        for state in states[: max(len(states) - keep, 0)]:
            shutil.rmtree(self.run_dir(state.run_id), ignore_errors=True)
            removed += 1
        return removed

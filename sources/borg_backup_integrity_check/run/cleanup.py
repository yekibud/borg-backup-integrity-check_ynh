"""LifecycleManager: destroy temporary cloud resources, recover from interrupted runs, find stale ones."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..errors import CleanupError, ProviderError
from ..logging_setup import get_logger
from ..providers.base import LABEL_RUN, CloudProvider, ManagedResource
from .state import RunState, RunStateStore

log = get_logger("cleanup")
STALE_AFTER = timedelta(hours=24)


@dataclass
class CleanupReport:
    destroyed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


class LifecycleManager:
    def __init__(self, provider: CloudProvider, store: RunStateStore, owner: str) -> None:
        self.provider = provider
        self.store = store
        self.owner = owner

    # ------------------------------------------------------------ one run
    def destroy_run(self, state: RunState, reason: str = "cleanup") -> CleanupReport:
        report = CleanupReport()
        state.cleanup_attempts += 1
        for volume_id in list(state.volume_ids):
            try:
                self.provider.destroy_volume(volume_id)
                report.destroyed.append(f"volume {volume_id}")
                state.volume_ids.remove(volume_id)
            except (ProviderError, NotImplementedError) as exc:
                report.failed.append(f"volume {volume_id}: {exc}")
        if state.server_id:
            try:
                self.provider.destroy_vm(state.server_id)
                report.destroyed.append(f"server {state.server_id} ({state.server_name})")
                state.server_id = None
            except ProviderError as exc:
                report.failed.append(f"server {state.server_id}: {exc}")
        # Volumes attached at creation could only be destroyed after the server is gone.
        for volume_id in list(state.volume_ids):
            try:
                self.provider.destroy_volume(volume_id)
                report.destroyed.append(f"volume {volume_id}")
                state.volume_ids.remove(volume_id)
            except (ProviderError, NotImplementedError) as exc:
                if not any(volume_id in f for f in report.failed):
                    report.failed.append(f"volume {volume_id}: {exc}")
        if report.ok:
            state.cleanup_status = "done"
            state.cleanup_error = None
            if state.status in ("running", "retained"):
                state.status = "finished"
            state.retained_until = None
        else:
            state.cleanup_status = "failed"
            state.cleanup_error = "; ".join(report.failed)
        state.notes.append(
            f"{datetime.now():%Y-%m-%d %H:%M} {reason}: destroyed {len(report.destroyed)}, failed {len(report.failed)}"
        )
        self.store.save(state)
        log.info(
            "cleanup of run %s: destroyed=%s failed=%s",
            state.run_id,
            report.destroyed,
            report.failed,
        )
        if not report.ok:
            raise CleanupError(state.cleanup_error or "cleanup failed")
        return report

    # ------------------------------------------------------- retained runs
    def expire_retained(self, now: datetime | None = None) -> list[CleanupReport]:
        now = now or datetime.now()
        reports = []
        for state in self.store.list():
            if (
                state.status == "retained"
                and state.retained_until
                and state.retained_until <= now
                and state.has_cloud_resources
            ):
                try:
                    reports.append(self.destroy_run(state, reason="retention expired"))
                except CleanupError as exc:
                    log.error("expired run %s could not be cleaned: %s", state.run_id, exc)
        return reports

    # ----------------------------------------------------- stale resources
    def find_stale(self, now: datetime | None = None) -> list[ManagedResource]:
        """Cloud resources we labelled that belong to no live/retained run (interrupted runs, crashes)."""
        now = now or datetime.now()
        states = {s.run_id: s for s in self.store.list()}
        stale: list[ManagedResource] = []
        for resource in self.provider.list_managed_resources(owner=self.owner):
            if resource.kind == "ssh_key":
                continue
            state = states.get(resource.run_id or "")
            if state is None:
                created = (
                    resource.created_at.replace(tzinfo=None)
                    if resource.created_at and resource.created_at.tzinfo
                    else resource.created_at
                )
                if created is None or now - created > STALE_AFTER:
                    stale.append(resource)
                continue
            if state.status == "retained" and state.retained_until and state.retained_until > now:
                continue
            if state.status == "running" and now - state.started_at < STALE_AFTER:
                continue
            stale.append(resource)
        return stale

    def destroy_resources(self, resources: list[ManagedResource]) -> CleanupReport:
        report = CleanupReport()
        for resource in sorted(resources, key=lambda r: 0 if r.kind == "server" else 1):
            try:
                if resource.kind == "server":
                    self.provider.destroy_vm(resource.id)
                elif resource.kind == "volume":
                    self.provider.destroy_volume(resource.id)
                else:
                    report.skipped.append(f"{resource.kind} {resource.id}")
                    continue
                report.destroyed.append(
                    f"{resource.kind} {resource.id} ({resource.name}, run {resource.run_id or '?'})"
                )
                if resource.run_id and (state := self.store.load(resource.run_id)):
                    if resource.kind == "server" and state.server_id == resource.id:
                        state.server_id = None
                    if resource.kind == "volume" and resource.id in state.volume_ids:
                        state.volume_ids.remove(resource.id)
                    if not state.has_cloud_resources:
                        state.cleanup_status = "done"
                        if state.status in ("running", "retained"):
                            state.status = "finished"
                    self.store.save(state)
            except (ProviderError, NotImplementedError) as exc:
                report.failed.append(f"{resource.kind} {resource.id}: {exc}")
        return report

    def verify_gone(self, state: RunState) -> list[str]:
        """Double-check with the provider that nothing labelled with this run id still exists."""
        leftovers = []
        for resource in self.provider.list_managed_resources(owner=self.owner):
            if resource.run_id == state.run_id and resource.kind in ("server", "volume"):
                leftovers.append(f"{resource.kind} {resource.id}")
        return leftovers


def resources_summary(resources: list[ManagedResource]) -> str:
    if not resources:
        return "none"
    return ", ".join(
        f"{r.kind} {r.name or r.id} ({r.extra.get('labels', {}).get(LABEL_RUN, r.run_id or '?')})"
        for r in resources
    )

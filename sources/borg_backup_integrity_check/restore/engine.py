"""CoreRestoreEngine: restore YunoHost system parts and app cores on the host through YunoHost itself."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..borg.listing import DirectoryAggregates
from ..discovery.components import Component
from ..logging_setup import get_logger
from ..report.models import FAIL, PASS, SKIPPED, WARN, ComponentReport, VerificationLevel
from .agent import HostAgent
from .planner import ComponentPlan, RestorePlan

log = get_logger("engine")


@dataclass
class CoreRestoreOutcome:
    ok: bool
    results: dict = field(default_factory=dict)
    log_path: str | None = None
    error: str | None = None


class CoreRestoreEngine:
    def __init__(
        self, agent: HostAgent, aggregates: dict[str, DirectoryAggregates], ssh_port: int
    ) -> None:
        self.agent = agent
        self.aggregates = aggregates  # archive name -> aggregates (for skeleton discovery)
        self.ssh_port = ssh_port

    # ------------------------------------------------------------- system
    def restore_system(self, plan: RestorePlan) -> dict[str, CoreRestoreOutcome]:
        """Restore all selected system config parts (they live in one archive) in a single YunoHost call."""
        outcomes: dict[str, CoreRestoreOutcome] = {}
        if not plan.system_conf:
            return outcomes
        by_archive: dict[str, list[ComponentPlan]] = {}
        for cp in plan.system_conf:
            by_archive.setdefault(cp.component.archive.name, []).append(cp)
        for archive, plans in by_archive.items():
            parts = [cp.component.id for cp in plans]
            spec = {
                "archive": archive,
                "targets": {"system": parts, "apps": []},
                "large_roots": [],
                "ssh_port": self.ssh_port,
                "force": True,
            }
            log.info("restoring system parts %s from %s", parts, archive)
            result = self.agent.call("restore-core", spec=spec, timeout=3 * 3600)
            outcome = _outcome(result)
            for cp in plans:
                part_result = ((outcome.results or {}).get("system") or {}).get(cp.component.id)
                outcomes[cp.component.id] = CoreRestoreOutcome(
                    ok=outcome.ok and part_result in (None, "Success", "Warning"),
                    results={"result": part_result},
                    log_path=outcome.log_path,
                    error=outcome.error,
                )
        return outcomes

    def ensure_domains(self, domains: list[str], main_domain: str | None) -> list[str]:
        """Without a restored system configuration, create the domains apps need (placeholder postinstall)."""
        notes: list[str] = []
        first = main_domain or (domains[0] if domains else "integrity-check.invalid")
        result = self.agent.call("postinstall", args=["--domain", first], timeout=1800)
        if not result.get("ok"):
            notes.append(
                f"placeholder post-install failed: {result.get('log_tail', result.get('error'))}"
            )
        self.agent.call("reopen-port", args=["--port", str(self.ssh_port)], timeout=300)
        for domain in domains:
            if domain == first:
                continue
            res = self.agent.call("add-domain", args=["--domain", domain], timeout=900)
            if not res.get("ok"):
                notes.append(
                    f"could not add domain {domain}: {res.get('log_tail', res.get('error'))}"
                )
        return notes

    def quarantine(self, domains: list[str]) -> list[str]:
        result = self.agent.call(
            "quarantine",
            args=["--domains", ",".join(domains), "--ssh-port", str(self.ssh_port)],
            timeout=600,
        )
        return result.get("actions", [])

    # ---------------------------------------------------------------- apps
    def restore_app(self, cp: ComponentPlan) -> CoreRestoreOutcome:
        comp = cp.component
        spec = {
            "archive": comp.archive.name,
            "targets": {"system": [], "apps": [comp.id]},
            "large_roots": [self._skeleton(comp, root.archive_path) for root in comp.large_roots],
            "ssh_port": self.ssh_port,
            "force": True,
        }
        log.info(
            "restoring app %s from %s (sparse: %d large root(s))",
            comp.id,
            comp.archive.name,
            len(comp.large_roots),
        )
        result = self.agent.call("restore-core", spec=spec, timeout=3 * 3600)
        outcome = _outcome(result)
        app_result = ((outcome.results or {}).get("apps") or {}).get(comp.id)
        if outcome.ok and app_result not in (None, "Success", "Warning"):
            outcome.ok = False
            outcome.error = f"YunoHost reported {app_result} for {comp.id}"
        outcome.results = {"result": app_result, "sparse_size": result.get("sparse_size")}
        return outcome

    def _skeleton(self, comp: Component, root_path: str) -> dict:
        agg = self.aggregates.get(comp.archive.name)
        skeleton = [{"path": root_path}]
        if agg is not None:
            for child, _ in agg.children(root_path):
                skeleton.append({"path": child})
        return {"archive_path": root_path, "skeleton": skeleton}

    # -------------------------------------------------------------- reports
    @staticmethod
    def apply_outcome(
        report: ComponentReport, outcome: CoreRestoreOutcome, what: str = "Core/configuration"
    ) -> None:
        if outcome.ok:
            detail = outcome.results.get("result") or ""
            report.add_check(
                what,
                PASS if detail != "Warning" else WARN,
                detail if detail == "Warning" else "",
                VerificationLevel.OBJECT_EXTRACTED,
            )
        else:
            report.add_check(
                what,
                FAIL,
                _failure_summary(outcome.error),
                VerificationLevel.ARCHIVE_METADATA_FOUND,
            )
        if outcome.log_path:
            report.operation_log = outcome.log_path

    @staticmethod
    def skipped(report: ComponentReport, reason: str) -> None:
        report.add_check("Core/configuration", SKIPPED, reason)


def _failure_summary(error: str | None, limit: int = 140) -> str:
    """One readable line out of a YunoHost restore failure.

    The raw material is a log tail: its first line is usually cut mid-word and most lines are
    warnings that say nothing about the cause, so prefer the last explicit ERROR line.
    """
    lines = [line.strip() for line in (error or "").splitlines() if line.strip()]
    if len(lines) > 1:
        lines = lines[1:]  # the tail starts mid-line
    errors = [line for line in lines if line.startswith("ERROR")]
    speaking = errors or [line for line in lines if not line.startswith("WARNING")] or lines
    best = re.sub(r"^(ERROR|WARNING|INFO)\s+", "", speaking[-1])
    best = " ".join(best.split())
    if not best:
        return "restore failed"
    return best if len(best) <= limit else best[: limit - 3] + "..."


def _outcome(result: dict) -> CoreRestoreOutcome:
    ok = bool(result.get("ok"))
    error = None
    if not ok:
        error = (
            result.get("error") or result.get("log_tail") or f"stage {result.get('stage')} failed"
        )
    return CoreRestoreOutcome(
        ok=ok, results=result.get("results") or {}, log_path=result.get("log"), error=error
    )

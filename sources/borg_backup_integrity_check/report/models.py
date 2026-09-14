"""Report data model with explicit verification levels (never overstate success)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any

from ..evidence.models import Evidence
from ..manifest.compare import Anomaly, Comparison
from ..manifest.models import BackupManifest


class VerificationLevel(IntEnum):
    NOT_CHECKED = 0
    ARCHIVE_METADATA_FOUND = 1
    OBJECT_LISTED = 2
    OBJECT_EXTRACTED = 3
    OBJECT_READABLE = 4  # parsed/recognised by the evidence layer
    SERVICE_RUNNING = 5
    HTTP_RESPONDING = 6
    REFERENCED_BY_APPLICATION = 7  # e.g. object name found in the restored app database
    VERIFIED_THROUGH_APPLICATION = 8  # retrieved through the app/protocol (IMAP, HTTP, API)

    @property
    def label(self) -> str:
        return {
            VerificationLevel.NOT_CHECKED: "NOT CHECKED",
            VerificationLevel.ARCHIVE_METADATA_FOUND: "ARCHIVE METADATA FOUND",
            VerificationLevel.OBJECT_LISTED: "LISTED IN ARCHIVE",
            VerificationLevel.OBJECT_EXTRACTED: "EXTRACTED",
            VerificationLevel.OBJECT_READABLE: "EXTRACTED AND READABLE",
            VerificationLevel.SERVICE_RUNNING: "SERVICE RUNNING",
            VerificationLevel.HTTP_RESPONDING: "HTTP RESPONDING",
            VerificationLevel.REFERENCED_BY_APPLICATION: "REFERENCED BY APPLICATION",
            VerificationLevel.VERIFIED_THROUGH_APPLICATION: "VERIFIED THROUGH APPLICATION",
        }[self]


PASS, WARN, FAIL, SKIPPED = "PASS", "WARN", "FAIL", "SKIPPED"


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | WARN | FAIL | SKIPPED
    detail: str = ""
    level: VerificationLevel = VerificationLevel.NOT_CHECKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "level": int(self.level),
        }


@dataclass
class SampleResult:
    evidence: Evidence
    level: VerificationLevel
    archive_path: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.evidence.readable

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence": self.evidence.to_dict(),
            "level": int(self.level),
            "archive_path": self.archive_path,
            "error": self.error,
        }


@dataclass
class ComponentReport:
    id: str
    label: str
    kind: str  # app | system_conf | system_data
    data_kind: str = "generic"  # mail | file | media | repo | generic | config
    status: str = SKIPPED
    checks: list[CheckResult] = field(default_factory=list)
    samples: list[SampleResult] = field(default_factory=list)
    sample_target: int = 0
    candidates: int = 0
    notes: list[str] = field(default_factory=list)
    large_roots: list[str] = field(default_factory=list)
    operation_log: str | None = None  # YunoHost operation log of the restore, on the restore host
    saved_log: str | None = None  # local copy of it, kept because the restore host is disposable

    @property
    def samples_readable(self) -> int:
        return sum(1 for s in self.samples if s.ok)

    @property
    def max_level(self) -> VerificationLevel:
        levels = [c.level for c in self.checks] + [s.level for s in self.samples]
        return max(levels) if levels else VerificationLevel.NOT_CHECKED

    @property
    def sample_level(self) -> VerificationLevel:
        levels = [s.level for s in self.samples if s.ok]
        return min(levels) if levels else VerificationLevel.NOT_CHECKED

    def check(self, name: str) -> CheckResult | None:
        for c in self.checks:
            if c.name == name:
                return c
        return None

    def add_check(
        self,
        name: str,
        status: str,
        detail: str = "",
        level: VerificationLevel = VerificationLevel.NOT_CHECKED,
    ) -> CheckResult:
        result = CheckResult(name, status, detail, level)
        self.checks.append(result)
        return result

    def finalize(self) -> None:
        statuses = [c.status for c in self.checks]
        if any(s == FAIL for s in statuses):
            self.status = FAIL
        elif self.samples and self.samples_readable < len(self.samples):
            self.status = WARN if self.samples_readable else FAIL
        elif any(s == WARN for s in statuses):
            self.status = WARN
        elif statuses or self.samples:
            self.status = PASS
        else:
            self.status = SKIPPED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "data_kind": self.data_kind,
            "status": self.status,
            "checks": [c.to_dict() for c in self.checks],
            "samples": [s.to_dict() for s in self.samples],
            "sample_target": self.sample_target,
            "candidates": self.candidates,
            "notes": list(self.notes),
            "large_roots": list(self.large_roots),
            "operation_log": self.operation_log,
            "saved_log": self.saved_log,
            "max_level": int(self.max_level),
        }


@dataclass
class RetainedHost:
    provider: str
    address: str
    ssh_port: int
    ssh_user: str
    expires_at: datetime | None
    domains: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    run_id: str
    mode: str
    started_at: datetime
    finished_at: datetime | None = None
    provider: str | None = None
    backup_time: datetime | None = None
    manifest: BackupManifest | None = None
    comparison: Comparison | None = None
    components: list[ComponentReport] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # errors requiring attention
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)
    borg_check: CheckResult | None = None
    deep_check: CheckResult | None = None
    cleanup_status: str = "not started"
    cleanup_error: str | None = None
    retained_host: RetainedHost | None = None
    fatal_error: str | None = None
    stages_completed: list[str] = field(default_factory=list)

    # ---- derived ------------------------------------------------------
    @property
    def core_failures(self) -> int:
        return sum(1 for c in self.components if c.status == FAIL and c.kind != "system_data")

    @property
    def sample_failures(self) -> int:
        return sum(len(c.samples) - c.samples_readable for c in self.components)

    @property
    def anomaly_warnings(self) -> list[Anomaly]:
        return self.comparison.warnings if self.comparison else []

    @property
    def anomaly_errors(self) -> list[Anomaly]:
        return self.comparison.errors if self.comparison else []

    @property
    def warning_count(self) -> int:
        return (
            len(self.warnings)
            + len(self.anomaly_warnings)
            + sum(1 for c in self.components if c.status == WARN)
        )

    @property
    def overall(self) -> str:
        if (
            self.fatal_error
            or self.problems
            or self.anomaly_errors
            or self.core_failures
            or self.cleanup_error
        ):
            return "FAIL"
        if any(c.status == FAIL for c in self.components):
            return "FAIL"
        if self.warning_count or self.sample_failures:
            return "PASS WITH WARNINGS"
        return "PASS"

    @property
    def overall_line(self) -> str:
        overall = self.overall
        if overall == "PASS WITH WARNINGS":
            n = self.warning_count + (1 if self.sample_failures else 0)
            return f"PASS WITH {n} WARNING{'S' if n != 1 else ''}"
        return overall

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "provider": self.provider,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "backup_time": self.backup_time.isoformat() if self.backup_time else None,
            "overall": self.overall,
            "components": [c.to_dict() for c in self.components],
            "problems": list(self.problems),
            "warnings": list(self.warnings),
            "infos": list(self.infos),
            "anomalies": [
                a.to_dict() for a in (self.comparison.anomalies if self.comparison else [])
            ],
            "borg_check": self.borg_check.to_dict() if self.borg_check else None,
            "deep_check": self.deep_check.to_dict() if self.deep_check else None,
            "cleanup_status": self.cleanup_status,
            "cleanup_error": self.cleanup_error,
            "fatal_error": self.fatal_error,
            "stages_completed": list(self.stages_completed),
            "retained_host": None
            if not self.retained_host
            else {
                "provider": self.retained_host.provider,
                "address": self.retained_host.address,
                "ssh_port": self.retained_host.ssh_port,
                "ssh_user": self.retained_host.ssh_user,
                "expires_at": self.retained_host.expires_at.isoformat()
                if self.retained_host.expires_at
                else None,
                "domains": list(self.retained_host.domains),
                "urls": list(self.retained_host.urls),
            },
            "manifest": self.manifest.to_dict() if self.manifest else None,
        }

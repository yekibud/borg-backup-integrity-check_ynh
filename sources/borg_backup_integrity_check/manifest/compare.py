"""Historical manifest comparison and anomaly detection.

Compares the current manifest against the immediately previous one and,
optionally, against rolling 7/30-day medians. Only comparable metrics are
compared (same metric present on both sides, same manifest flavour); otherwise
the row is marked "not comparable" instead of inventing a percentage.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..units import format_bytes, format_count, format_pct, pct_change
from .models import BackupManifest, ComponentMetrics


@dataclass
class Thresholds:
    growth_pct: float = 40.0
    shrink_pct: float = 20.0
    count_growth_pct: float = 40.0
    count_shrink_pct: float = 20.0
    min_bytes_for_pct: int = 50 * 1024 * 1024  # ignore % swings on tiny components
    min_files_for_pct: int = 200
    db_dump_shrink_pct: float = 50.0
    max_backup_age_hours: float = 36.0


@dataclass
class Anomaly:
    severity: str  # warning | error
    message: str
    component: str | None = None
    metric: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ComparisonRow:
    label: str
    metric: str  # size | count | db_dump
    current: float | None
    previous: float | None
    change_pct: float | None
    comparable: bool = True
    anomaly: Anomaly | None = None
    component: str | None = None

    def fmt_current(self) -> str:
        return (
            format_count(int(self.current))
            if self.metric == "count" and self.current is not None
            else format_bytes(self.current)
        )

    def fmt_previous(self) -> str:
        return (
            format_count(int(self.previous))
            if self.metric == "count" and self.previous is not None
            else format_bytes(self.previous)
        )

    def fmt_change(self) -> str:
        if not self.comparable:
            return "n/c"
        return format_pct(self.change_pct)


@dataclass
class BaselineRow:
    label: str
    metric: str
    current: float | None
    baseline: float | None
    days: int
    change_pct: float | None
    samples: int
    anomaly: Anomaly | None = None


@dataclass
class Comparison:
    previous: BackupManifest | None
    size_rows: list[ComparisonRow] = field(default_factory=list)
    count_rows: list[ComparisonRow] = field(default_factory=list)
    baseline_rows: list[BaselineRow] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_change_pct(self) -> float | None:
        for row in self.size_rows:
            if row.component is None and row.label.startswith("Total"):
                return row.change_pct if row.comparable else None
        return None

    @property
    def warnings(self) -> list[Anomaly]:
        return [a for a in self.anomalies if a.severity == "warning"]

    @property
    def errors(self) -> list[Anomaly]:
        return [a for a in self.anomalies if a.severity == "error"]


class ManifestComparator:
    def __init__(self, thresholds: Thresholds | None = None) -> None:
        self.t = thresholds or Thresholds()

    # ------------------------------------------------------------- public
    def compare(
        self,
        current: BackupManifest,
        previous: BackupManifest | None,
        history: list[BackupManifest] | None = None,
        now: datetime | None = None,
    ) -> Comparison:
        cmp = Comparison(previous=previous)
        self._check_age(current, cmp, now)
        if previous is None:
            cmp.notes.append("No previous manifest available: this run establishes the baseline.")
            return cmp
        self._compare_totals(current, previous, cmp)
        self._compare_components(current, previous, cmp)
        self._compare_missing(current, previous, cmp)
        for days in (7, 30):
            self._rolling(current, history or [], days, cmp)
        return cmp

    # ------------------------------------------------------------ internal
    def _check_age(self, current: BackupManifest, cmp: Comparison, now: datetime | None) -> None:
        now = now or datetime.now()
        age = now - current.backup_time
        if age > timedelta(hours=self.t.max_backup_age_hours):
            hours = age.total_seconds() / 3600
            cmp.anomalies.append(
                Anomaly(
                    "error",
                    f"No sufficiently recent backup exists: newest generation is {hours:.0f} hours old (limit {self.t.max_backup_age_hours:.0f}h).",
                    metric="age",
                )
            )

    def _compare_totals(
        self, current: BackupManifest, previous: BackupManifest, cmp: Comparison
    ) -> None:
        comparable = current.stats_only == previous.stats_only or previous.stats_only
        # When the previous manifest is stats-only, compare Borg original sizes on both sides.
        if previous.stats_only:
            cur_total = sum(a.original_size or 0 for a in current.archives) or None
            cur_files = sum(a.nfiles or 0 for a in current.archives) or None
            cmp.notes.append(
                "Previous manifest only has Borg archive statistics; comparing archive-level sizes."
            )
        else:
            cur_total, cur_files = current.total_logical, current.total_files
        prev_total, prev_files = previous.total_logical, previous.total_files
        row = self._row("Total logical data", "size", cur_total, prev_total, comparable)
        self._flag(row, cmp)
        cmp.size_rows.append(row)
        crow = self._row("Total objects", "count", cur_files, prev_files, comparable)
        self._flag(crow, cmp)
        cmp.count_rows.append(crow)

    def _compare_components(
        self, current: BackupManifest, previous: BackupManifest, cmp: Comparison
    ) -> None:
        if previous.stats_only or current.stats_only:
            # Only archive-level statistics are comparable.
            for archive in current.archives:
                prev = previous.archive_for(archive.component)
                if prev is None:
                    continue
                row = self._row(
                    archive.component,
                    "size",
                    archive.original_size,
                    prev.original_size,
                    True,
                    archive.component,
                )
                self._flag(row, cmp)
                cmp.size_rows.append(row)
                crow = self._row(
                    archive.component, "count", archive.nfiles, prev.nfiles, True, archive.component
                )
                self._flag(crow, cmp)
                cmp.count_rows.append(crow)
            return
        for comp_id, metrics in current.components.items():
            prev = previous.components.get(comp_id)
            label = metrics.label or comp_id
            if prev is None:
                cmp.size_rows.append(
                    ComparisonRow(
                        label,
                        "size",
                        metrics.logical_size,
                        None,
                        None,
                        comparable=False,
                        component=comp_id,
                    )
                )
                cmp.notes.append(f"{label}: new component, nothing to compare with yet.")
                continue
            row = self._row(label, "size", metrics.logical_size, prev.logical_size, True, comp_id)
            self._flag(row, cmp)
            cmp.size_rows.append(row)
            crow = self._row(label, "count", metrics.file_count, prev.file_count, True, comp_id)
            self._flag(crow, cmp)
            cmp.count_rows.append(crow)
            self._compare_db_dump(label, metrics, prev, cmp)

    def _compare_db_dump(
        self, label: str, cur: ComponentMetrics, prev: ComponentMetrics, cmp: Comparison
    ) -> None:
        if not prev.db_dump_size and not cur.db_dump_size:
            return
        if prev.db_dump_size and not cur.db_dump_size:
            cmp.anomalies.append(
                Anomaly(
                    "error",
                    f"{label}: database dump present in the previous backup is missing or empty now.",
                    cur.id,
                    "db_dump",
                )
            )
            return
        change = pct_change(cur.db_dump_size, prev.db_dump_size)
        if (
            change is not None
            and change <= -self.t.db_dump_shrink_pct
            and prev.db_dump_size > 1024 * 1024
        ):
            cmp.anomalies.append(
                Anomaly(
                    "warning",
                    f"{label}: database dump changed from {format_bytes(prev.db_dump_size)} to {format_bytes(cur.db_dump_size)} ({format_pct(change)}).",
                    cur.id,
                    "db_dump",
                )
            )

    def _compare_missing(
        self, current: BackupManifest, previous: BackupManifest, cmp: Comparison
    ) -> None:
        current_components = {a.component for a in current.archives}
        for archive in previous.archives:
            if archive.component not in current_components:
                cmp.anomalies.append(
                    Anomaly(
                        "error",
                        f"Archive for component '{archive.component}' existed in the previous backup ({archive.start:%Y-%m-%d %H:%M}) but is missing now.",
                        archive.component,
                        "missing",
                    )
                )

    def _rolling(
        self, current: BackupManifest, history: list[BackupManifest], days: int, cmp: Comparison
    ) -> None:
        window = [
            m
            for m in history
            if not m.stats_only
            and m.backup_time < current.backup_time
            and m.backup_time >= current.backup_time - timedelta(days=days)
        ]
        if len(window) < 3 or current.stats_only:
            return
        for comp_id, metrics in current.components.items():
            values = [m.components[comp_id].logical_size for m in window if comp_id in m.components]
            if len(values) < 3:
                continue
            baseline = statistics.median(values)
            change = pct_change(metrics.logical_size, baseline)
            label = metrics.label or comp_id
            row = BaselineRow(
                label, "size", metrics.logical_size, baseline, days, change, len(values)
            )
            if change is not None and baseline >= self.t.min_bytes_for_pct:
                if change >= self.t.growth_pct:
                    row.anomaly = Anomaly(
                        "warning",
                        f"{label} increased {format_pct(change, signed=False)} versus its {days}-day baseline.",
                        comp_id,
                        "baseline",
                    )
                elif change <= -self.t.shrink_pct:
                    row.anomaly = Anomaly(
                        "warning",
                        f"{label} decreased {format_pct(abs(change), signed=False)} versus its {days}-day baseline.",
                        comp_id,
                        "baseline",
                    )
                if row.anomaly:
                    cmp.anomalies.append(row.anomaly)
            cmp.baseline_rows.append(row)

    def _row(
        self, label: str, metric: str, cur, prev, comparable: bool, component: str | None = None
    ) -> ComparisonRow:
        if cur is None or prev is None:
            return ComparisonRow(
                label, metric, cur, prev, None, comparable=False, component=component
            )
        return ComparisonRow(
            label,
            metric,
            cur,
            prev,
            pct_change(cur, prev),
            comparable=comparable,
            component=component,
        )

    def _flag(self, row: ComparisonRow, cmp: Comparison) -> None:
        if not row.comparable or row.change_pct is None or row.previous is None:
            return
        if row.metric == "size":
            if row.previous < self.t.min_bytes_for_pct:
                return
            up, down, noun = self.t.growth_pct, self.t.shrink_pct, "data"
        else:
            if row.previous < self.t.min_files_for_pct:
                return
            up, down, noun = self.t.count_growth_pct, self.t.count_shrink_pct, "object count"
        if row.change_pct >= up:
            row.anomaly = Anomaly(
                "warning",
                f"{row.label} {noun} increased {format_pct(row.change_pct, signed=False)} since the previous backup ({row.fmt_previous()} -> {row.fmt_current()}).",
                row.component,
                row.metric,
            )
        elif row.change_pct <= -down:
            row.anomaly = Anomaly(
                "warning",
                f"{row.label} {noun} decreased {format_pct(abs(row.change_pct), signed=False)} since the previous backup ({row.fmt_previous()} -> {row.fmt_current()}).",
                row.component,
                row.metric,
            )
        if row.anomaly:
            cmp.anomalies.append(row.anomaly)

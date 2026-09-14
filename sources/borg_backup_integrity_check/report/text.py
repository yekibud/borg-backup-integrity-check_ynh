"""Plain-text report renderer: what failed first, then the evidence, in clearly marked sections."""

from __future__ import annotations

import textwrap
from datetime import datetime

from ..units import format_bytes, format_count, format_pct
from .models import FAIL, WARN, ComponentReport, RunReport, VerificationLevel

WIDTH = 78
DATA_KIND_TITLES = {
    "mail": "MAIL-LIKE COMPONENT",
    "file": "FILE-LIKE APPLICATION",
    "media": "MEDIA-LIKE APPLICATION",
    "repo": "REPOSITORY-LIKE APPLICATION",
    "config": "SYSTEM CONFIGURATION",
    "generic": "APPLICATION",
}
KIND_TITLES = {"system_data": "SYSTEM DATA", "system_conf": "SYSTEM CONFIGURATION"}
# Short "what kind of problem" labels for the headline block.
CHECK_KINDS = {
    "Core/configuration": "core restore",
    "Restore": "core restore",
    "Service health": "service",
    "HTTP health": "http",
    "Login endpoint": "login",
    "Database restore": "database",
    "Mail server access": "mail",
}
LABEL_COL = 19
KIND_COL = 15


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "n/a"


def _one_line(text: str, room: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= room else text[: room - 3] + "..."


def _wrapped(text: str, indent: int) -> list[str]:
    # Never split a long word: URLs and paths in a report are there to be copied.
    return textwrap.wrap(
        text,
        WIDTH,
        subsequent_indent=" " * indent,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [text]


def render_report(report: RunReport) -> str:
    lines: list[str] = []
    add = lines.append
    add("#" * WIDTH)
    add(f"# YUNOHOST BORG BACKUP INTEGRITY CHECK - {_fmt_dt(report.started_at)}")
    add("#" * WIDTH)
    add("")
    add(f"OVERALL: {report.overall_line}")
    add("")
    _headline_block(report, add)
    _section(add, "RUN SUMMARY")
    _summary_block(report, add)
    _section(add, "BACKUP MANIFEST COMPARISON")
    _manifest_block(report, add)
    if report.borg_check or report.deep_check:
        _section(add, "BORG-LEVEL CHECKS")
        for check in (report.borg_check, report.deep_check):
            if check:
                add(f"{check.name + ':':<26}{check.status}  {check.detail}".rstrip())
    if report.components:
        _section(add, "COMPONENTS")
        add("Service, HTTP and login checks run inside the isolated restore server: its")
        add("/etc/hosts maps every restored domain to 127.0.0.1 and curl resolves ports 80 and")
        add("443 there, so a production host name in a URL below was answered by the restore")
        add("server - the live server is never contacted.")
        add("")
        for component in report.components:
            _component_block(component, add)
            add("")
        lines.pop()
    if any(c.operation_log for c in report.components):
        _section(add, "RESTORE OPERATION LOGS (on the restore server)")
        _operation_logs_block(report, add)
    if report.retained_host:
        _section(add, "RETAINED RESTORE SERVER (manual inspection)")
        _retained_block(report, add)
    return "\n".join(lines).rstrip() + "\n"


def summary_of(text: str) -> str:
    """The head of a rendered report: verdict, what failed, run summary - no evidence.

    Used by the config panel, so that what the web admin shows is literally the first screen
    of the emailed report.
    """
    lines = text.splitlines()
    banners = [i for i, line in enumerate(lines) if line.startswith("#### ")]
    end = banners[1] if len(banners) > 1 else min(len(lines), 40)
    return "\n".join(lines[:end]).rstrip() + "\n"


def _section(add, title: str) -> None:
    add("")
    add(f"#### {title} " + "#" * max(3, WIDTH - len(title) - 6))
    add("")


# --------------------------------------------------------------- what failed
def _headline_items(
    report: RunReport,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    failures: list[tuple[str, str, str]] = []
    attention: list[tuple[str, str, str]] = []
    if report.fatal_error:
        failures.append(("run", "fatal", report.fatal_error))
    if report.cleanup_error:
        failures.append(
            (
                "cleanup",
                "cloud cost",
                f"temporary resources may still exist: {report.cleanup_error}",
            )
        )
    failures += [("run", "error", problem) for problem in report.problems]
    failures += [(a.component or "backup", "manifest", a.message) for a in report.anomaly_errors]
    for component in report.components:
        for check in component.checks:
            kind = CHECK_KINDS.get(check.name, check.name.lower())
            if check.status == FAIL:
                failures.append((component.label, kind, check.detail or "failed"))
            elif check.status == WARN:
                attention.append((component.label, kind, check.detail or "warning"))
        unreadable = len(component.samples) - component.samples_readable
        if component.samples and unreadable:
            row = (
                component.label,
                "samples",
                f"{unreadable} of {len(component.samples)} sampled objects unreadable",
            )
            (failures if component.status == FAIL else attention).append(row)
    attention += [(a.component or "backup", "manifest", a.message) for a in report.anomaly_warnings]
    attention += [("run", "warning", warning) for warning in report.warnings]
    return failures, attention


def _headline_rows(label: str, kind: str, detail: str) -> list[str]:
    head = f"  {label[: LABEL_COL - 1]:<{LABEL_COL}}{kind[: KIND_COL - 1]:<{KIND_COL}}"
    return _wrapped(head + " ".join(str(detail).split()), len(head))


def _headline_block(report: RunReport, add) -> None:
    failures, attention = _headline_items(report)
    if not failures and not attention:
        add("Nothing needs attention: every checked component restored and verified.")
        add("")
        return
    for title, rows in (("WHAT FAILED", failures), ("NEEDS ATTENTION", attention)):
        if not rows:
            continue
        add(f"{title} ({len(rows)})")
        add("")
        for row in rows:
            for line in _headline_rows(*row):
                add(line)
        add("")


# ------------------------------------------------------------------- summary
def _summary_block(report: RunReport, add) -> None:
    manifest, cmp = report.manifest, report.comparison
    previous = cmp.previous if cmp else None
    add(f"{'Mode:':<24}{report.mode}" + (f" ({report.provider})" if report.provider else ""))
    add(f"{'Backup:':<24}{_fmt_dt(report.backup_time)}")
    if manifest:
        size = format_bytes(manifest.total_logical)
        if manifest.total_deduplicated is not None:
            size += f" logical, {format_bytes(manifest.total_deduplicated)} stored"
        add(f"{'Size:':<24}{size}")
    if previous:
        add(
            f"{'Previous:':<24}{format_bytes(previous.total_logical)} ({_fmt_dt(previous.backup_time)}), "
            f"change {format_pct(cmp.total_change_pct) if cmp else 'n/a'}"
        )
    else:
        add(f"{'Previous:':<24}none (baseline run)")
    checked = sum(1 for c in report.components if c.status != "SKIPPED")
    add(
        f"{'Components:':<24}{checked} checked, {report.core_failures} restore failure(s), "
        f"{report.sample_failures} sample failure(s), {report.warning_count} warning(s)"
    )
    if report.cleanup_error:
        add(f"{'Cleanup:':<24}FAILED - {report.cleanup_error}")
    else:
        add(f"{'Cleanup:':<24}{report.cleanup_status}")


# ------------------------------------------------------------------ manifest
def _manifest_block(report: RunReport, add) -> None:
    cmp = report.comparison
    if cmp is None or cmp.previous is None:
        add("No previous backup manifest to compare with (baseline established by this run).")
        if cmp:
            for note in cmp.notes:
                add(f"NOTE: {note}")
            for anomaly in cmp.anomalies:
                add(f"{anomaly.severity.upper()}: {anomaly.message}")
        return
    counts = {row.component: row for row in cmp.count_rows}
    add(f"{'Component':<24}{'Size now':>12}{'Change':>9}{'Objects now':>14}{'Change':>9}")
    for row in cmp.size_rows:
        count = counts.get(row.component)
        flagged = row.anomaly or (count.anomaly if count else None)
        add(
            f"{row.label[:23]:<24}{row.fmt_current():>12}{row.fmt_change():>9}"
            f"{(count.fmt_current() if count else '-'):>14}"
            f"{(count.fmt_change() if count else '-'):>9}"
            f"{'  WARNING' if flagged else ''}"
        )
    if cmp.baseline_rows:
        flagged_rows = [r for r in cmp.baseline_rows if r.anomaly]
        if flagged_rows:
            add("")
            add(f"{'Component':<24}{'Current':>12}{'Baseline':>12}{'Change':>9}  Window")
            for row in flagged_rows:
                add(
                    f"{row.label[:23]:<24}{format_bytes(row.current):>12}"
                    f"{format_bytes(row.baseline):>12}{format_pct(row.change_pct):>9}"
                    f"  {row.days}-day median  WARNING"
                )
    if cmp.anomalies or cmp.notes:
        add("")
    for anomaly in cmp.anomalies:
        for line in _wrapped(f"{anomaly.severity.upper()}: {anomaly.message}", 2):
            add(line)
    for note in cmp.notes:
        for line in _wrapped(f"NOTE: {note}", 2):
            add(line)


# ----------------------------------------------------------------- components
def _component_header(title: str, status: str) -> str:
    left = f"== {title} "
    right = f" {status} =="
    return left + "=" * max(3, WIDTH - len(left) - len(right)) + right


def _component_block(component: ComponentReport, add) -> None:
    title = DATA_KIND_TITLES.get(component.data_kind, "APPLICATION")
    if component.data_kind == "generic" and component.kind in KIND_TITLES:
        title = KIND_TITLES[component.kind]
    add(_component_header(f"{title}: {component.label}", component.status))
    for check in component.checks:
        head = f"{check.name + ':':<26}{check.status}"
        for line in _wrapped(f"{head}  {check.detail}" if check.detail else head, 28):
            add(line)
    if component.sample_target and not component.samples and not component.candidates:
        add(f"{'Recent objects:':<26}none (no sampleable objects in this backup)")
    elif component.sample_target or component.samples:
        add(
            f"{'Recent objects:':<26}{component.samples_readable}/{len(component.samples)} readable"
            + (
                f" (target {component.sample_target}, {format_count(component.candidates)} candidates)"
                if component.candidates
                else ""
            )
        )
        if component.samples:
            level = component.sample_level
            add(
                f"{'Sample verification:':<26}{level.label if level else VerificationLevel.NOT_CHECKED.label}"
            )
    if component.samples:
        add("")
        add(f"Recent backed-up objects ({len(component.samples)}):")
        ordered = sorted(
            component.samples,
            key=lambda s: s.evidence.when.timestamp() if s.evidence.when else 0,
            reverse=True,
        )
        for sample in ordered:
            add(_sample_line(sample))
    for note in component.notes:
        for line in _wrapped(f"NOTE: {note}", 2):
            add(line)


def _sample_line(sample) -> str:
    ev = sample.evidence
    when = _fmt_dt(ev.when)
    if ev.kind == "email":
        who = f"  from {ev.details['from']}" if ev.details.get("from") else ""
        text = f'{when}    "{ev.title}"{who}'
    elif ev.kind == "git_repo":
        commit = ev.details.get("last_commit")
        text = f"{when}    {ev.title}/  (git" + (f", last commit {commit}" if commit else "") + ")"
    else:
        extra = ""
        if ev.details.get("width") and ev.details.get("height"):
            extra = f"  {ev.details['width']}x{ev.details['height']}"
        if ev.details.get("duration_seconds"):
            extra += f"  {ev.details['duration_seconds']}s"
        rel = ev.details.get("relative_path") or ev.title
        text = f"{when}    {rel}{extra}"
    if not sample.ok:
        text += f"    !! {sample.error or ev.error or 'unreadable'}"
    elif sample.level >= VerificationLevel.REFERENCED_BY_APPLICATION:
        text += (
            "    [app]"
            if sample.level == VerificationLevel.REFERENCED_BY_APPLICATION
            else "    [verified]"
        )
    return text


def _operation_logs_block(report: RunReport, add) -> None:
    """One entry per YunoHost restore operation, with the components it restored."""
    grouped: dict[str, list[str]] = {}
    for component in report.components:
        if component.operation_log:
            grouped.setdefault(component.operation_log, []).append(component.label)
    for path, labels in grouped.items():
        add(_one_line(", ".join(labels), WIDTH))
        add(f"    {path}")


def _retained_block(report: RunReport, add) -> None:
    host = report.retained_host
    assert host is not None
    add(f"{'Provider:':<16}{host.provider}")
    add(f"{'Address:':<16}{host.address}")
    add(f"{'SSH:':<16}ssh -p {host.ssh_port} {host.ssh_user}@{host.address}")
    if host.expires_at:
        add(
            f"{'Destroyed at:':<16}{_fmt_dt(host.expires_at)} (or run: borg-backup-integrity-check destroy)"
        )
    if host.domains:
        add("")
        add("To browse restored applications, map the domains to the server in /etc/hosts:")
        add(f"    {host.address} {' '.join(host.domains)}")
    for url in host.urls:
        add(f"    {url}")

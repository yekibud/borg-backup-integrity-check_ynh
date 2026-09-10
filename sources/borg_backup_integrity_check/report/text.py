"""Plain-text report renderer: summary and manifest comparison first, per-component details after."""

from __future__ import annotations

from datetime import datetime

from ..units import format_bytes, format_count, format_pct
from .models import ComponentReport, RunReport, VerificationLevel

WIDTH = 78
DATA_KIND_TITLES = {
    "mail": "MAIL-LIKE COMPONENT",
    "file": "FILE-LIKE APPLICATION",
    "media": "MEDIA-LIKE APPLICATION",
    "repo": "REPOSITORY-LIKE APPLICATION",
    "config": "SYSTEM CONFIGURATION",
    "generic": "APPLICATION",
}


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "n/a"


def render_report(report: RunReport) -> str:
    lines: list[str] = []
    add = lines.append
    add("YunoHost Borg Backup Integrity Check")
    add(_fmt_dt(report.started_at))
    add("")
    add(f"OVERALL: {report.overall_line}")
    add("")
    _summary_block(report, add)
    add("")
    _manifest_block(report, add)
    add("")
    if report.borg_check or report.deep_check:
        add("BORG-LEVEL CHECKS")
        add("")
        for check in (report.borg_check, report.deep_check):
            if check:
                add(f"{check.name + ':':<26}{check.status}  {check.detail}".rstrip())
        add("")
    for component in report.components:
        _component_block(component, add)
        add("")
    _attention_block(report, add)
    if report.retained_host:
        _retained_block(report, add)
    return "\n".join(lines).rstrip() + "\n"


def _summary_block(report: RunReport, add) -> None:
    manifest, cmp = report.manifest, report.comparison
    previous = cmp.previous if cmp else None
    add(f"{'Mode:':<24}{report.mode}" + (f" ({report.provider})" if report.provider else ""))
    add(f"{'Backup:':<24}{_fmt_dt(report.backup_time)}")
    if manifest:
        add(f"{'Logical data:':<24}{format_bytes(manifest.total_logical)}")
        if manifest.total_deduplicated is not None:
            add(f"{'Deduplicated (stored):':<24}{format_bytes(manifest.total_deduplicated)}")
    if previous:
        add(
            f"{'Previous:':<24}{format_bytes(previous.total_logical)}  ({_fmt_dt(previous.backup_time)})"
        )
        add(f"{'Change:':<24}{format_pct(cmp.total_change_pct) if cmp else 'n/a'}")
    else:
        add(f"{'Previous:':<24}none (baseline run)")
    add("")
    checked = sum(1 for c in report.components if c.status != "SKIPPED")
    add(f"{'Apps/components:':<24}{checked} checked")
    add(f"{'Core restore failures:':<24}{report.core_failures}")
    add(f"{'Sample failures:':<24}{report.sample_failures}")
    add(f"{'Warnings:':<24}{report.warning_count}")
    if report.cleanup_error:
        add(f"{'Cleanup:':<24}FAILED - {report.cleanup_error}")
    else:
        add(f"{'Cleanup:':<24}{report.cleanup_status}")


def _manifest_block(report: RunReport, add) -> None:
    add("BACKUP MANIFEST COMPARISON")
    add("")
    cmp = report.comparison
    if cmp is None or cmp.previous is None:
        add("No previous backup manifest to compare with (baseline established by this run).")
        if cmp:
            for note in cmp.notes:
                add(f"NOTE: {note}")
            for anomaly in cmp.anomalies:
                add(f"{anomaly.severity.upper()}: {anomaly.message}")
        return
    add(f"{'Component':<26}{'Current':>13}{'Previous':>13}{'Change':>10}")
    for row in cmp.size_rows:
        flag = "  " + ("WARNING" if row.anomaly else "")
        add(
            f"{row.label[:25]:<26}{row.fmt_current():>13}{row.fmt_previous():>13}{row.fmt_change():>10}{flag}".rstrip()
        )
    if cmp.count_rows:
        add("")
        add(f"{'Component':<26}{'Objects now':>13}{'Previous':>13}{'Change':>10}")
        for row in cmp.count_rows:
            flag = "  " + ("WARNING" if row.anomaly else "")
            add(
                f"{row.label[:25]:<26}{row.fmt_current():>13}{row.fmt_previous():>13}{row.fmt_change():>10}{flag}".rstrip()
            )
    if cmp.baseline_rows:
        flagged = [r for r in cmp.baseline_rows if r.anomaly]
        if flagged:
            add("")
            add(f"{'Component':<26}{'Current':>13}{'Baseline':>13}{'Change':>10}  Window")
            for row in flagged:
                add(
                    f"{row.label[:25]:<26}{format_bytes(row.current):>13}{format_bytes(row.baseline):>13}{format_pct(row.change_pct):>10}  {row.days}-day median  WARNING"
                )
    add("")
    for anomaly in cmp.anomalies:
        add(f"{anomaly.severity.upper()}: {anomaly.message}")
    for note in cmp.notes:
        add(f"NOTE: {note}")


def _component_block(component: ComponentReport, add) -> None:
    title = DATA_KIND_TITLES.get(component.data_kind, "APPLICATION")
    add(f"{title}: {component.label}")
    add(component.status)
    add("")
    for check in component.checks:
        detail = f"  {check.detail}" if check.detail else ""
        add(f"{check.name + ':':<26}{check.status}{detail}")
    if component.sample_target or component.samples:
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
        add("Recent backed-up objects:")
        add("")
        for sample in component.samples:
            add(_sample_line(sample))
    for note in component.notes:
        add(f"NOTE: {note}")


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


def _attention_block(report: RunReport, add) -> None:
    items: list[str] = []
    if report.fatal_error:
        items.append(f"FATAL: {report.fatal_error}")
    items += [f"ERROR: {p}" for p in report.problems]
    items += [f"ERROR: {a.message}" for a in report.anomaly_errors]
    items += [f"WARNING: {a.message}" for a in report.anomaly_warnings]
    items += [f"WARNING: {w}" for w in report.warnings]
    for component in report.components:
        if component.status == "FAIL":
            failed = [c for c in component.checks if c.status == "FAIL"]
            detail = "; ".join(f"{c.name}: {c.detail}" for c in failed) or "see component section"
            items.append(f"ERROR: {component.label}: {detail}")
        elif component.status == "WARN":
            if component.samples and component.samples_readable < len(component.samples):
                items.append(
                    f"WARNING: {component.label}: {len(component.samples) - component.samples_readable} of {len(component.samples)} sampled objects unreadable"
                )
            for c in component.checks:
                if c.status == "WARN":
                    items.append(f"WARNING: {component.label}: {c.name}: {c.detail}")
    if report.cleanup_error:
        items.append(
            f"ERROR: CLEANUP FAILED - temporary cloud resources may still exist and cost money: {report.cleanup_error}"
        )
    add("ATTENTION REQUIRED" if items else "ATTENTION REQUIRED: nothing")
    add("")
    for item in items:
        add(f"- {item}")


def _retained_block(report: RunReport, add) -> None:
    host = report.retained_host
    assert host is not None
    add("")
    add("RETAINED RESTORE SERVER (manual inspection)")
    add("")
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

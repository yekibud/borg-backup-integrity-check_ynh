"""HTML rendering of a run report, for the e-mail's alternative part.

Phone mail clients rewrap ``text/plain``, which destroys the aligned columns of the plain
renderer. This one is built from the report model instead of the text, uses one column and
inline styles only (no <style>, no media query, no image, no script: what mail clients keep).
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from ..units import format_bytes, format_pct
from .models import FAIL, PASS, SKIPPED, WARN, ComponentReport, RunReport
from .text import DATA_KIND_TITLES, KIND_TITLES, _headline_items

STATUS_COLOURS = {
    PASS: "#1e6b3a",
    WARN: "#8a6d00",
    FAIL: "#b3261e",
    SKIPPED: "#5f6368",
}
BODY = "margin:0;padding:16px;background:#f5f5f5;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#202124;font-size:15px;line-height:1.45"
CARD = "background:#ffffff;border:1px solid #e0e0e0;border-radius:8px;padding:12px 14px;margin:0 0 12px"
MONO = "font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;word-break:break-word"
MUTED = "color:#5f6368;font-size:13px"


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "n/a"


def _status_badge(status: str) -> str:
    colour = STATUS_COLOURS.get(status, "#5f6368")
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
        f'background:{colour};color:#ffffff;font-size:12px;font-weight:600">{escape(status)}</span>'
    )


def _problem_rows(rows: list[tuple[str, str, str]], colour: str) -> str:
    out = []
    for label, kind, detail in rows:
        out.append(
            f'<div style="border-left:3px solid {colour};padding:0 0 0 10px;margin:0 0 10px">'
            f'<div style="font-weight:600">{escape(label)}'
            f'<span style="{MUTED}"> &middot; {escape(kind)}</span></div>'
            f'<div style="{MONO};color:#3c4043">{escape(" ".join(str(detail).split()))}</div>'
            f"</div>"
        )
    return "".join(out)


def _summary_rows(report: RunReport) -> list[tuple[str, str]]:
    manifest, cmp = report.manifest, report.comparison
    previous = cmp.previous if cmp else None
    rows = [
        ("Mode", report.mode + (f" ({report.provider})" if report.provider else "")),
        ("Backup", _fmt_dt(report.backup_time)),
    ]
    if manifest:
        size = format_bytes(manifest.total_logical)
        if manifest.total_deduplicated is not None:
            size += f" logical, {format_bytes(manifest.total_deduplicated)} stored"
        rows.append(("Size", size))
    if previous:
        rows.append(
            (
                "Previous",
                f"{format_bytes(previous.total_logical)} ({_fmt_dt(previous.backup_time)}), "
                f"change {format_pct(cmp.total_change_pct) if cmp else 'n/a'}",
            )
        )
    else:
        rows.append(("Previous", "none (baseline run)"))
    checked = sum(1 for c in report.components if c.status != SKIPPED)
    rows.append(
        (
            "Components",
            f"{checked} checked, {report.core_failures} restore failure(s), "
            f"{report.sample_failures} sample failure(s), {report.warning_count} warning(s)",
        )
    )
    rows.append(("Cleanup", report.cleanup_error or report.cleanup_status))
    return rows


def _component_card(component: ComponentReport) -> str:
    title = DATA_KIND_TITLES.get(component.data_kind, "APPLICATION")
    if component.data_kind == "generic" and component.kind in KIND_TITLES:
        title = KIND_TITLES[component.kind]
    parts = [
        f'<div style="{CARD}">',
        f'<div style="display:flex;justify-content:space-between;align-items:center;gap:8px">'
        f'<div><span style="{MUTED}">{escape(title)}</span><br>'
        f'<span style="font-size:17px;font-weight:600">{escape(component.label)}</span></div>'
        f"<div>{_status_badge(component.status)}</div></div>",
    ]
    for check in component.checks:
        colour = STATUS_COLOURS.get(check.status, "#5f6368")
        detail = (
            f'<div style="{MONO};color:#3c4043">{escape(check.detail)}</div>'
            if check.detail
            else ""
        )
        parts.append(
            f'<div style="margin:10px 0 0">'
            f'<span style="font-weight:600">{escape(check.name)}</span> '
            f'<span style="color:{colour};font-weight:600">{escape(check.status)}</span>'
            f"{detail}</div>"
        )
    if component.samples:
        parts.append(
            f'<div style="margin:12px 0 4px;font-weight:600">Recent backed-up objects '
            f"({len(component.samples)})</div>"
        )
        ordered = sorted(
            component.samples,
            key=lambda s: s.evidence.when.timestamp() if s.evidence.when else 0,
            reverse=True,
        )
        for sample in ordered:
            evidence = sample.evidence
            suffix = ""
            if not sample.ok:
                suffix = f' <span style="color:{STATUS_COLOURS[FAIL]}">unreadable</span>'
            parts.append(
                f'<div style="{MONO};margin:0 0 4px">'
                f'<span style="{MUTED}">{escape(_fmt_dt(evidence.when))}</span> '
                f"{escape(evidence.details.get('relative_path') or evidence.title)}{suffix}</div>"
            )
    for note in component.notes:
        parts.append(f'<div style="{MUTED};margin:8px 0 0">{escape(note)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _manifest_table(report: RunReport) -> str:
    cmp = report.comparison
    if cmp is None or cmp.previous is None:
        return (
            f'<div style="{CARD}"><div style="font-weight:600">Backup manifest</div>'
            f'<div style="{MUTED}">No previous manifest to compare with '
            f"(baseline established by this run).</div></div>"
        )
    counts = {row.component: row for row in cmp.count_rows}
    head = (
        '<tr style="text-align:left">'
        '<th style="padding:4px 6px 4px 0">Component</th>'
        '<th style="padding:4px 6px;text-align:right">Size</th>'
        '<th style="padding:4px 6px;text-align:right">Change</th>'
        '<th style="padding:4px 0 4px 6px;text-align:right">Objects</th></tr>'
    )
    rows = []
    for row in cmp.size_rows:
        count = counts.get(row.component)
        flagged = row.anomaly or (count.anomaly if count else None)
        colour = STATUS_COLOURS[WARN] if flagged else "#202124"
        rows.append(
            f'<tr style="color:{colour}">'
            f'<td style="padding:3px 6px 3px 0;border-top:1px solid #eeeeee">{escape(row.label)}</td>'
            f'<td style="padding:3px 6px;border-top:1px solid #eeeeee;text-align:right">{escape(row.fmt_current())}</td>'
            f'<td style="padding:3px 6px;border-top:1px solid #eeeeee;text-align:right">{escape(row.fmt_change())}</td>'
            f'<td style="padding:3px 0 3px 6px;border-top:1px solid #eeeeee;text-align:right">'
            f"{escape(count.fmt_current() if count else '-')}</td></tr>"
        )
    anomalies = "".join(
        f'<div style="{MUTED};margin:8px 0 0">{escape(a.severity.upper())}: {escape(a.message)}</div>'
        for a in cmp.anomalies
    )
    return (
        f'<div style="{CARD}">'
        f'<div style="font-weight:600;margin:0 0 6px">Backup manifest comparison</div>'
        f'<table style="width:100%;border-collapse:collapse;font-size:13px">{head}{"".join(rows)}</table>'
        f"{anomalies}</div>"
    )


def render_html(report: RunReport) -> str:
    failures, attention = _headline_items(report)
    overall_colour = (
        STATUS_COLOURS[FAIL]
        if report.overall == FAIL
        else STATUS_COLOURS.get(PASS if report.overall == PASS else WARN, "#5f6368")
    )
    parts = [
        f'<!doctype html><html><body style="{BODY}">',
        f'<div style="{CARD}">',
        f'<div style="{MUTED}">YunoHost Borg backup integrity check &middot; '
        f"{escape(_fmt_dt(report.started_at))}</div>",
        f'<div style="font-size:22px;font-weight:700;color:{overall_colour};margin:4px 0 0">'
        f"{escape(report.overall_line)}</div>",
        "</div>",
    ]
    if failures:
        parts.append(
            f'<div style="{CARD}"><div style="font-weight:600;margin:0 0 10px">'
            f"What failed ({len(failures)})</div>{_problem_rows(failures, STATUS_COLOURS[FAIL])}</div>"
        )
    if attention:
        parts.append(
            f'<div style="{CARD}"><div style="font-weight:600;margin:0 0 10px">'
            f"Needs attention ({len(attention)})</div>"
            f"{_problem_rows(attention, STATUS_COLOURS[WARN])}</div>"
        )
    if not failures and not attention:
        parts.append(f'<div style="{CARD}">Every checked component restored and verified.</div>')
    summary = "".join(
        f'<tr><td style="padding:3px 10px 3px 0;{MUTED};white-space:nowrap;vertical-align:top">'
        f'{escape(label)}</td><td style="padding:3px 0">{escape(value)}</td></tr>'
        for label, value in _summary_rows(report)
    )
    parts.append(
        f'<div style="{CARD}"><div style="font-weight:600;margin:0 0 6px">Run summary</div>'
        f'<table style="width:100%;border-collapse:collapse">{summary}</table></div>'
    )
    parts.append(_manifest_table(report))
    for check in (report.borg_check, report.deep_check):
        if check:
            parts.append(
                f'<div style="{CARD}"><span style="font-weight:600">{escape(check.name)}</span> '
                f'{_status_badge(check.status)}<div style="{MUTED}">{escape(check.detail)}</div></div>'
            )
    if report.components:
        parts.append(
            f'<div style="{MUTED};margin:0 0 8px">Service, HTTP and login checks ran inside the '
            f"isolated restore server; the production host names in the URLs below resolve to it, "
            f"so the live server was never contacted.</div>"
        )
        parts.extend(_component_card(component) for component in report.components)
    if report.retained_host:
        host = report.retained_host
        parts.append(
            f'<div style="{CARD}"><div style="font-weight:600">Retained restore server</div>'
            f'<div style="{MONO}">ssh -p {host.ssh_port} {escape(host.ssh_user)}@{escape(host.address)}</div>'
            f'<div style="{MUTED}">destroyed at {escape(_fmt_dt(host.expires_at))}</div></div>'
        )
    parts.append("</body></html>")
    return "".join(parts)

"""The e-mail a phone receives: subject that says what failed, HTML alternative, multipart body."""

from __future__ import annotations

import subprocess
from datetime import datetime

from borg_backup_integrity_check.evidence.models import Evidence
from borg_backup_integrity_check.report import mail
from borg_backup_integrity_check.report.html import render_html
from borg_backup_integrity_check.report.models import (
    FAIL,
    PASS,
    WARN,
    ComponentReport,
    RunReport,
    SampleResult,
    VerificationLevel,
)

T0 = datetime(2026, 9, 15, 3, 2)


def _report(*components: ComponentReport) -> RunReport:
    report = RunReport(
        run_id="20260915-030219-tn4p",
        mode="sampled",
        started_at=T0,
        provider="hetzner",
        backup_time=datetime(2026, 9, 15, 0, 24),
    )
    report.components.extend(components)
    report.cleanup_status = "all temporary resources destroyed"
    return report


def _failed(app: str, detail: str = "Could not restore") -> ComponentReport:
    component = ComponentReport(id=app, label=app, kind="app")
    component.add_check("Core/configuration", FAIL, detail)
    component.finalize()
    return component


def test_subject_names_the_failing_components():
    report = _report(*(_failed(app) for app in ("immich", "jitsi", "rspamdui", "synapse")))
    subject = report.email_subject("borg-backup-integrity-check")

    assert subject.startswith("[borg-backup-integrity-check] FAIL: immich, jitsi, rspamdui +1")
    assert subject.endswith("- backup 2026-09-15 00:24")


def test_subject_falls_back_to_warnings_then_to_the_verdict_alone():
    warned = ComponentReport(id="nextcloud", label="nextcloud", kind="app")
    warned.add_check("HTTP health", WARN, "HTTP 503")
    warned.finalize()
    assert "nextcloud" in _report(warned).email_subject("bbic")

    healthy = ComponentReport(id="wordpress", label="wordpress", kind="app")
    healthy.add_check("HTTP health", PASS, "200")
    healthy.finalize()
    subject = _report(healthy).email_subject("bbic")
    assert subject == "[bbic] PASS - backup 2026-09-15 00:24"


def test_html_leads_with_the_failures_and_escapes_their_text():
    report = _report(_failed("immich", 'chown "<script>alert(1)</script>": No such file'))
    html = render_html(report)

    assert html.startswith("<!doctype html>")
    assert html.index("What failed") < html.index("Run summary")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "immich" in html and "FAIL" in html


def test_html_lists_sampled_objects_and_says_nothing_failed_when_nothing_did():
    component = ComponentReport(id="nextcloud", label="nextcloud", kind="app", data_kind="file")
    component.add_check("Database restore", PASS, "264 tables in nextcloud")
    component.samples.append(
        SampleResult(
            Evidence(
                "image",
                "IMG_1.jpg",
                T0,
                details={"relative_path": "data/tony/files/Media/IMG_1.jpg"},
            ),
            VerificationLevel.OBJECT_READABLE,
            "apps/nextcloud/backup/data/tony/files/Media/IMG_1.jpg",
        )
    )
    component.finalize()
    html = render_html(_report(component))

    assert "data/tony/files/Media/IMG_1.jpg" in html
    assert "Every checked component restored and verified." in html


def test_send_report_builds_a_multipart_alternative(monkeypatch):
    captured: dict[str, bytes] = {}

    def fake_run(cmd, input=None, **kwargs):  # noqa: A002 - mirrors subprocess.run
        captured["cmd"] = cmd
        captured["input"] = input
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(mail.subprocess, "run", fake_run)
    mail.send_report("root", "[bbic] FAIL: immich", "plain body", html="<html>rich body</html>")

    message = captured["input"].decode("utf-8", "replace")
    assert "Content-Type: multipart/alternative" in message
    assert "plain body" in message and "rich body" in message
    assert message.index("plain body") < message.index("rich body")  # fallback first


def test_send_report_without_html_stays_plain(monkeypatch):
    captured: dict[str, bytes] = {}

    def fake_run(cmd, input=None, **kwargs):  # noqa: A002 - mirrors subprocess.run
        captured["input"] = input
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(mail.subprocess, "run", fake_run)
    mail.send_report("root", "[bbic] PASS", "plain body")

    message = captured["input"].decode("utf-8", "replace")
    assert "multipart" not in message and "plain body" in message

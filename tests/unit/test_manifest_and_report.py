from datetime import datetime, timedelta

from borg_backup_integrity_check.evidence.models import Evidence
from borg_backup_integrity_check.manifest.compare import ManifestComparator, Thresholds
from borg_backup_integrity_check.manifest.history import HistoryStore
from borg_backup_integrity_check.manifest.models import (
    ArchiveMetrics,
    BackupManifest,
    ComponentMetrics,
)
from borg_backup_integrity_check.report.models import (
    PASS,
    ComponentReport,
    RetainedHost,
    RunReport,
    SampleResult,
    VerificationLevel,
)
from borg_backup_integrity_check.report.text import render_report

T0 = datetime(2026, 9, 10, 2, 1)


def _manifest(
    when: datetime,
    sizes: dict[str, tuple[int, int]],
    db: dict[str, int] | None = None,
    stats_only=False,
) -> BackupManifest:
    m = BackupManifest(
        generation_id=when.strftime("%Y%m%dT%H%M%S"),
        backup_time=when,
        generated_at=when + timedelta(hours=7),
        stats_only=stats_only,
    )
    for comp, (size, files) in sizes.items():
        m.archives.append(
            ArchiveMetrics(
                component=f"auto_{comp}",
                name=f"auto_{comp}-{when:%Y-%m-%dT%H:%M:%S}",
                start=when,
                original_size=size,
                compressed_size=size // 2,
                deduplicated_size=size // 10,
                nfiles=files,
            )
        )
        if not stats_only:
            m.components[comp] = ComponentMetrics(
                id=comp,
                kind="app",
                archive=f"auto_{comp}",
                logical_size=size,
                file_count=files,
                core_size=size // 10,
                core_files=10,
                large_size=size - size // 10,
                large_files=files - 10,
                db_dump_size=(db or {}).get(comp, 0),
                label=comp.capitalize(),
            )
    return m


def test_comparison_flags_growth_shrink_missing_and_db_dump():
    prev = _manifest(
        T0 - timedelta(days=1),
        {
            "files": (246_800_000_000, 81_982),
            "photos": (10_100_000_000, 9_104),
            "mail": (18_000_000_000, 54_001),
            "wiki": (500_000_000, 3000),
        },
        db={"files": 2_100_000_000},
    )
    cur = _manifest(
        T0,
        {
            "files": (247_100_000_000, 82_413),
            "photos": (15_800_000_000, 14_120),
            "mail": (18_200_000_000, 54_213),
        },
        db={"files": 17_000_000},
    )
    cmp = ManifestComparator(Thresholds()).compare(cur, prev, now=T0 + timedelta(hours=7))
    messages = [a.message for a in cmp.anomalies]
    assert any("Photos data increased 56.4%" in m for m in messages)
    assert any("Photos object count increased 55.1%" in m for m in messages)
    assert any("database dump changed from 2.1 GB to 17.0 MB" in m for m in messages)
    assert any("auto_wiki" in m and "missing" in m for m in messages)
    assert not any("Files" in m and "increased" in m for m in messages)
    total = next(r for r in cmp.size_rows if r.label.startswith("Total"))
    assert round(total.change_pct, 1) == 2.1
    assert cmp.errors and cmp.warnings


def test_comparison_shrink_and_age_error():
    prev = _manifest(T0 - timedelta(days=1), {"files": (100_000_000_000, 82_413)})
    cur = _manifest(T0, {"files": (58_000_000_000, 12_204)})
    cmp = ManifestComparator().compare(cur, prev, now=T0 + timedelta(days=3))
    msgs = [a.message for a in cmp.anomalies]
    assert any("decreased 42.0%" in m for m in msgs)
    assert any("Object count dropped" in m or "object count decreased" in m for m in msgs)
    assert any("No sufficiently recent backup" in m for m in msgs)


def test_comparison_ignores_small_fluctuations_and_tiny_components():
    prev = _manifest(
        T0 - timedelta(days=1), {"files": (100_000_000_000, 80_000), "tiny": (1_000_000, 50)}
    )
    cur = _manifest(T0, {"files": (101_500_000_000, 80_400), "tiny": (3_000_000, 150)})
    cmp = ManifestComparator().compare(cur, prev, now=T0 + timedelta(hours=1))
    assert cmp.anomalies == []


def test_stats_only_baseline_is_compared_at_archive_level_and_marked():
    prev = _manifest(T0 - timedelta(days=1), {"files": (100_000_000_000, 80_000)}, stats_only=True)
    cur = _manifest(T0, {"files": (150_000_000_000, 80_000)})
    cmp = ManifestComparator().compare(cur, prev, now=T0 + timedelta(hours=1))
    assert any("archive-level" in n for n in cmp.notes)
    assert any("increased 50.0%" in a.message for a in cmp.anomalies)
    assert all(r.comparable for r in cmp.size_rows if r.label.startswith("Total"))


def test_rolling_baseline_warns_versus_median():
    history = [
        _manifest(T0 - timedelta(days=d), {"mail": (18_000_000_000 + d * 1000, 54_000)})
        for d in range(1, 8)
    ]
    cur = _manifest(T0, {"mail": (29_700_000_000, 54_000)})
    cmp = ManifestComparator().compare(
        cur, history[0], history=history, now=T0 + timedelta(hours=1)
    )
    baseline = [r for r in cmp.baseline_rows if r.days == 7]
    assert baseline and baseline[0].anomaly is not None
    assert (
        "7-day baseline" in baseline[0].anomaly.message and "65.0%" in baseline[0].anomaly.message
    )


def test_history_store_roundtrip_and_prune(tmp_path):
    store = HistoryStore(tmp_path / "history")
    old = _manifest(T0 - timedelta(days=500), {"a": (1, 1)})
    prev = _manifest(T0 - timedelta(days=1), {"a": (1, 1)})
    cur = _manifest(T0, {"a": (2, 2)})
    for m in (old, prev, cur):
        path = store.save(m)
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert [m.generation_id for m in store.load_all()] == [
        old.generation_id,
        prev.generation_id,
        cur.generation_id,
    ]
    assert store.previous_to(cur).generation_id == prev.generation_id
    assert store.known_components() == ["auto_a"]
    assert store.prune(400, now=T0) == 1
    assert len(store.window(cur, 7)) == 1
    loaded = store.latest()
    assert loaded.components["a"].logical_size == 2 and loaded.total_files == 2


def test_report_rendering_puts_summary_and_comparison_first():
    prev = _manifest(
        T0 - timedelta(days=1),
        {"files": (246_800_000_000, 81_982), "photos": (10_100_000_000, 9_104)},
    )
    cur = _manifest(T0, {"files": (247_100_000_000, 82_413), "photos": (15_800_000_000, 14_120)})
    cmp = ManifestComparator().compare(cur, prev, now=T0 + timedelta(hours=7))
    report = RunReport(
        run_id="20260910-090000-ab12",
        mode="sampled",
        started_at=T0 + timedelta(hours=7),
        provider="hetzner",
        backup_time=T0,
        manifest=cur,
        comparison=cmp,
    )
    files = ComponentReport(
        id="files", label="files", kind="app", data_kind="file", sample_target=20, candidates=82000
    )
    files.add_check("Core/configuration", PASS, level=VerificationLevel.SERVICE_RUNNING)
    files.add_check(
        "HTTP health", PASS, "200 https://example.org/files/", VerificationLevel.HTTP_RESPONDING
    )
    files.samples.append(
        SampleResult(
            Evidence(
                "file",
                "contract.pdf",
                T0 + timedelta(hours=5, minutes=53),
                details={"relative_path": "Documents/contract.pdf"},
            ),
            VerificationLevel.OBJECT_READABLE,
            "apps/files/backup/x/Documents/contract.pdf",
        )
    )
    files.samples.append(
        SampleResult(
            Evidence(
                "image",
                "IMG_4821.jpg",
                T0 - timedelta(hours=5),
                details={"relative_path": "Photos/IMG_4821.jpg", "width": 4032, "height": 3024},
            ),
            VerificationLevel.REFERENCED_BY_APPLICATION,
            "apps/files/backup/x/Photos/IMG_4821.jpg",
        )
    )
    files.samples.append(
        SampleResult(
            Evidence("file", "broken.bin", T0, readable=False, error="short read"),
            VerificationLevel.OBJECT_EXTRACTED,
            "apps/files/backup/x/broken.bin",
            error="short read",
        )
    )
    files.finalize()
    mail = ComponentReport(id="data_mail", label="Mail data", kind="system_data", data_kind="mail")
    mail.samples.append(
        SampleResult(
            Evidence(
                "email",
                "Re: Tuesday meeting",
                T0 + timedelta(hours=6, minutes=30),
                details={"from": "Alice"},
            ),
            VerificationLevel.VERIFIED_THROUGH_APPLICATION,
            "data/mail/alice/cur/1",
        )
    )
    mail.finalize()
    report.components += [files, mail]
    report.cleanup_status = "all temporary resources destroyed"
    report.retained_host = RetainedHost(
        "hetzner",
        "203.0.113.10",
        22022,
        "root",
        T0 + timedelta(hours=11),
        ["example.org"],
        ["https://example.org/files/"],
    )
    text = render_report(report)
    lines = text.splitlines()
    assert lines[1].startswith("# YUNOHOST BORG BACKUP INTEGRITY CHECK")
    assert "OVERALL: PASS WITH" in text
    assert text.index("BACKUP MANIFEST COMPARISON") < text.index("FILE-LIKE APPLICATION: files")
    assert "Photos" in text and "+56.4%" in text and "+55.1%  WARNING" in text
    assert 'Re: Tuesday meeting"  from Alice' in text and "[verified]" in text
    assert "Documents/contract.pdf" in text and "4032x3024" in text and "[app]" in text
    assert "!! short read" in text
    assert "Recent objects:" in text and "2/3 readable" in text
    assert "203.0.113.10 example.org" in text
    # What needs attention comes before the evidence, not after it.
    assert "NEEDS ATTENTION" in text and "1 of 3 sampled objects unreadable" in text
    assert text.index("NEEDS ATTENTION") < text.index("MAIL-LIKE COMPONENT")


def test_overall_status_rules():
    r = RunReport(run_id="x", mode="sampled", started_at=T0)
    assert r.overall == "PASS"
    c = ComponentReport(id="a", label="a", kind="app")
    c.add_check("Core/configuration", "FAIL", "restore script failed")
    c.finalize()
    r.components.append(c)
    assert r.overall == "FAIL" and r.core_failures == 1
    r2 = RunReport(run_id="y", mode="sampled", started_at=T0, cleanup_error="server 1 still exists")
    assert r2.overall == "FAIL"
    r3 = RunReport(run_id="z", mode="sampled", started_at=T0, warnings=["x"])
    assert r3.overall_line == "PASS WITH 1 WARNING"


def test_http_failure_is_warning_in_sampled_mode_but_fail_in_full():
    from borg_backup_integrity_check.report.models import ComponentReport
    from borg_backup_integrity_check.restore.health import ApplicationHealthChecker

    result = {
        "http": {"code": 503, "url": "https://nc.example.org/nextcloud/"},
        "sso": {"code": 302, "url": "x"},
    }
    sampled = ComponentReport(id="nextcloud", label="nextcloud", kind="app")
    ApplicationHealthChecker._apply_http(result, sampled, None, sampled=True)
    http = sampled.check("HTTP health")
    assert http.status == "WARN" and "expected when bulk data is absent" in http.detail
    full = ComponentReport(id="nextcloud", label="nextcloud", kind="app")
    ApplicationHealthChecker._apply_http(result, full, None, sampled=False)
    assert full.check("HTTP health").status == "FAIL"
    # A genuinely dead endpoint (no code) is still flagged.
    dead = ComponentReport(id="x", label="x", kind="app")
    ApplicationHealthChecker._apply_http(
        {"http": {"code": 0, "url": "u"}}, dead, None, sampled=True
    )
    assert dead.check("HTTP health").status == "WARN"

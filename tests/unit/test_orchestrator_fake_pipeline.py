"""Level-1 architecture proof: the whole run pipeline with fakes for Borg, the provider and the host."""

from __future__ import annotations

import gzip
import json
import struct
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from tests.conftest import NOW, dir_items, make_item

from borg_backup_integrity_check.borg.models import ArchiveRef, ArchiveStats, RepositoryInfo
from borg_backup_integrity_check.config import AppConfig, Paths
from borg_backup_integrity_check.evidence.extractors import EvidenceExtractor
from borg_backup_integrity_check.providers.base import ManagedResource, ProviderCapabilities
from borg_backup_integrity_check.providers.static import StaticHostProvider
from borg_backup_integrity_check.report.models import VerificationLevel
from borg_backup_integrity_check.run import orchestrator as orch
from borg_backup_integrity_check.run.state import RunStateStore

T_PREVIOUS = NOW - timedelta(days=1, hours=7)
T_BACKUP = NOW - timedelta(hours=7)
T_NEWER = NOW - timedelta(hours=1)


def _jpeg(width=640, height=480):
    sof0 = struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8\xff\xc0" + sof0 + b"\xff\xd9"


def _email(i):
    return f"From: Alice <alice@example.org>\nSubject: Weekend plans {i}\nDate: Thu, 10 Sep 2026 0{i % 9}:31:00 +0200\nMessage-ID: <m{i}@example.org>\n\nsecret body\n".encode()


# ----------------------------------------------------------------------- fakes
class FakeBorgClient:
    """Serves one synthetic backup generation (conf + data + filebox app) plus an older one."""

    def __init__(self, listings, layouts, **kwargs):
        self.listings = listings
        self.layouts = layouts
        self.kwargs = kwargs
        self.calls: list[str] = []

    def version(self):
        return "1.4.5"

    def list_archives(self):
        names = ["auto_conf", "auto_data", "auto_filebox"]
        refs = []
        for generation in (T_PREVIOUS, T_BACKUP):
            for i, n in enumerate(names):
                start = generation + timedelta(minutes=i)
                refs.append(
                    ArchiveRef(
                        name=f"{n}-{start:%Y-%m-%dT%H:%M:%S}",
                        id=f"{n}{generation:%Y%m%d%H%M}",
                        start=start,
                    )
                )
        return refs

    def archive_stats(self, archive):
        listing = self.listings[archive.split("-")[0]]
        size = sum(i.size for i in listing if i.is_file)
        scale = 0.9 if f"{T_PREVIOUS:%Y-%m-%dT%H:%M}" in archive else 1.0
        return ArchiveStats(
            int(size * scale),
            int(size * scale / 2),
            int(size * scale / 10),
            sum(1 for i in listing if i.is_file),
        )

    def repository_info(self):
        return RepositoryInfo("repoid", "ssh://fake/repo", None, "repokey")

    def cache_listing(self, archive, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(target, "wt") as fh:
            for it in self.listings[archive.split("-")[0]]:
                fh.write(
                    json.dumps(
                        {
                            "path": it.path,
                            "type": it.type,
                            "size": it.size,
                            "mtime": it.mtime.isoformat(),
                            "user": it.user,
                            "group": it.group,
                            "healthy": True,
                        }
                    )
                    + "\n"
                )
        return target

    def check(self, **kwargs):
        self.calls.append("check")

    def extract_from_list(self, archive, dest, paths, dry_run=False, **kw):
        self.calls.append(f"dryrun:{len(list(paths))}")

    def extract(self, *a, **k):
        self.calls.append("extract")


class FakeProvider(StaticHostProvider):
    name = "fakecloud"
    display_name = "Fake Cloud"
    capabilities = ProviderCapabilities(
        regions=False,
        sizes=False,
        volumes=False,
        pricing=False,
        user_data=False,
        ssh_keys_registry=False,
    )

    def __init__(self):
        super().__init__("198.51.100.5", 22022)
        self.live: dict[str, str] = {}
        self.destroyed: list[str] = []

    def create_vm(self, spec):
        vm = super().create_vm(spec)
        vm.id = "srv-1"
        self.live[vm.id] = spec.name
        self.spec = spec
        return vm

    def get_vm(self, vm_id):
        return super().get_vm(vm_id) if vm_id in self.live else None

    def destroy_vm(self, vm_id):
        self.live.pop(vm_id, None)
        self.destroyed.append(vm_id)

    def list_managed_resources(self, owner=None):
        return [
            ManagedResource("server", sid, name, name.replace("bbic-", ""), owner, datetime.now())
            for sid, name in self.live.items()
        ]


class FakeSSH:
    def __init__(self, host, port, key_path, known_hosts, user="root"):
        self.host, self.port = host, port

    def wait_ready(self, timeout=0):
        return None

    def run(self, *a, **k):
        return None


class FakeAgent:
    """Emulates the on-host helper: 'extracts' sampled objects into a fake host root and describes them."""

    def __init__(self, ssh, host_root: Path, fail_app: str | None = None):
        self.ssh = ssh
        self.host_root = host_root
        self.fail_app = fail_app
        self.calls: list[tuple[str, dict | None]] = []

    def deploy(self):
        return None

    def call(self, command, spec=None, args=None, timeout=0):
        self.calls.append((command, spec))
        if command == "restore-core":
            apps = spec["targets"]["apps"]
            if apps and apps[0] == self.fail_app:
                return {
                    "ok": False,
                    "stage": "restore",
                    "error": "app restore script failed: db import error",
                    "log": "/var/log/yunohost/operations/20260914-201242-backup_restore_app.yml",
                    "log_text": "INFO starting\nERROR mysql: Access denied for user\n",
                }
            return {
                "ok": True,
                "results": {
                    "system": {p: "Success" for p in spec["targets"]["system"]},
                    "apps": {a: "Success" for a in apps},
                },
                "log": "/var/log/yunohost/categories/operation/x.log",
                "sparse_size": 123,
            }
        if command == "extract-payload":
            roots = []
            for root in spec["roots"]:
                objs = []
                for obj in root["objects"]:
                    live = self.host_root / obj["live_path"].lstrip("/")
                    live.parent.mkdir(parents=True, exist_ok=True)
                    name = live.name
                    if name.endswith(".jpg"):
                        live.write_bytes(_jpeg())
                    elif name.endswith(".odt"):
                        with zipfile.ZipFile(live, "w") as zf:
                            zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
                            zf.writestr("content.xml", "<x/>")
                    elif "/cur/" in obj["live_path"] or "/new/" in obj["live_path"]:
                        live.write_bytes(_email(int(name.split(".")[0])))
                    elif name == "broken.pdf":
                        live.write_bytes(b"")
                    else:
                        live.write_bytes(b"%PDF-1.7\n%fake\n")
                    objs.append(
                        {
                            "archive_path": obj["archive_path"],
                            "live_path": obj["live_path"],
                            "kind": obj["kind"],
                            "extracted": name != "missing.pdf",
                        }
                    )
                roots.append(
                    {
                        "archive_path": root["archive_path"],
                        "live_path": root["live_path"],
                        "rc": 0,
                        "error": None,
                        "objects": objs,
                    }
                )
            return {"ok": True, "roots": roots}
        if command == "describe":
            ex = EvidenceExtractor()
            out = []
            for obj in spec["objects"]:
                path = self.host_root / obj["live_path"].lstrip("/")
                ev = ex.describe(
                    path,
                    kind_hint=obj["kind"],
                    mtime=datetime.fromisoformat(obj["mtime"]) if obj.get("mtime") else None,
                    size=obj.get("size"),
                    display_path=obj.get("relative_path"),
                )
                d = ev.to_dict()
                d["details"]["relative_path"] = obj["relative_path"]
                out.append({"archive_path": obj["archive_path"], "evidence": d})
            return {"ok": True, "objects": out}
        if command == "health":
            return {
                "services": [],
                "registered_services": [{"name": spec["app"], "status": "running"}],
                "http": {"url": f"https://{spec['domain']}{spec['path']}/", "code": 200},
                "sso": {"url": "https://x/yunohost/sso/", "code": 200},
                "db": {"ok": True, "tables": 42},
                "db_refs": [n for n in spec["basenames"] if n.startswith("IMG_")],
            }
        if command == "mail-verify":
            return {
                "ok": True,
                "objects": [
                    {"archive_path": o["archive_path"], "verified": True} for o in spec["objects"]
                ],
            }
        if command == "app-settings":
            return {
                "ok": True,
                "settings": {"domain": "example.org", "path": "/files", "db_name": "filebox"},
            }
        if command == "quarantine":
            return {"ok": True, "actions": ["hosts", "postfix sink"]}
        if command == "borg-test":
            return {"ok": True, "archives": [f"auto_conf-{T_BACKUP:%Y-%m-%dT%H:%M:%S}"]}
        return {"ok": True, "rc": 0}


# --------------------------------------------------------------------- fixture
@pytest.fixture
def pipeline(tmp_path, monkeypatch, synthetic_app_listing, synthetic_app_layout):
    from borg_backup_integrity_check.borg.layout import BackupCsvRow, BackupInfo, BackupLayout

    conf_listing = (
        dir_items("conf/ldap")
        + dir_items("conf/ynh")
        + [
            make_item("conf/ldap/dc=yunohost-dc=org.ldif", 90_000, user="root"),
            make_item("conf/ynh/current_host", 20, user="root"),
            make_item("conf/ynh/settings.yml", 3000, user="root"),
        ]
    )
    conf_info = BackupInfo.from_dict(
        {
            "created_at": 1,
            "size": 93020,
            "size_details": {"system": {"conf_ldap": 90000, "conf_ynh_settings": 3020}, "apps": {}},
            "apps": {},
            "system": {
                "conf_ldap": {"paths": ["conf/ldap"]},
                "conf_ynh_settings": {"paths": ["conf/ynh"]},
                "conf_manually_modified_files": {"paths": ["conf/manually_modified_files"]},
            },
            "from_yunohost_version": "12.1.41",
        }
    )
    conf_layout = BackupLayout("auto_conf", conf_info)
    data_listing = (
        dir_items("data/mail/alice/cur")
        + dir_items("data/mail/bob/new")
        + [
            make_item(f"data/mail/alice/cur/{i}.eml", 800, NOW - timedelta(hours=i), user="vmail")
            for i in range(30)
        ]
        + [
            make_item(f"data/mail/bob/new/{i}.eml", 700, NOW - timedelta(hours=i * 2), user="vmail")
            for i in range(30, 40)
        ]
    )
    data_info = BackupInfo.from_dict(
        {
            "created_at": 1,
            "size": 1,
            "size_details": {"system": {"data_mail": 1}, "apps": {}},
            "apps": {},
            "system": {"data_mail": {"paths": ["data/mail"]}},
            "from_yunohost_version": "12.1.41",
        }
    )
    data_layout = BackupLayout("auto_data", data_info, [BackupCsvRow("/var/mail", "data/mail")])
    app_listing = list(synthetic_app_listing) + [
        make_item(
            "apps/filebox/backup/home/yunohost.app/filebox/bob/files/broken.pdf",
            10,
            NOW + timedelta(minutes=1),
        ),
        make_item(
            "apps/filebox/backup/home/yunohost.app/filebox/bob/files/missing.pdf",
            10,
            NOW + timedelta(minutes=2),
        ),
    ]
    listings = {"auto_conf": conf_listing, "auto_data": data_listing, "auto_filebox": app_listing}
    layouts = {
        "auto_conf": conf_layout,
        "auto_data": data_layout,
        "auto_filebox": synthetic_app_layout,
    }

    paths = Paths(app_id="borg-backup-integrity-check")
    paths.settings_file = tmp_path / "settings.yml"
    paths.etc_dir = tmp_path / "etc"
    paths.data_dir = tmp_path / "data"
    paths.log_dir = tmp_path / "log"
    paths.install_dir = tmp_path / "www"
    paths.keys_dir.mkdir(parents=True)
    paths.restore_host_key.write_text("private")
    Path(str(paths.restore_host_key) + ".pub").write_text("ssh-ed25519 AAAA test")
    paths.settings_file.write_text(
        yaml.safe_dump(
            {
                "id": "borg-backup-integrity-check",
                "cloud_provider": "fakecloud",
                "use_borg_ynh": "0",
                "borg_repository": "ssh://u@h/./repo",
                "sample_size": 10,
                "deep_check_sample": 20,
            }
        )
    )
    config = AppConfig.load(paths)
    config.secrets.set("borg_passphrase", "pp-secret")

    fake_client = FakeBorgClient(listings, layouts)
    provider = FakeProvider()
    host_root = tmp_path / "host"
    agents: list[FakeAgent] = []
    sent: list[tuple[str, str, str]] = []
    state = {"fail_app": None}

    monkeypatch.setenv("BBIC_BORG_BINARY", "/bin/true")
    monkeypatch.setattr(orch, "BorgClient", lambda **kw: fake_client)
    monkeypatch.setattr(
        orch, "read_layout", lambda client, archive, workdir: layouts[archive.split("-")[0]]
    )
    monkeypatch.setattr(orch, "create_provider", lambda name, creds, settings: provider)
    monkeypatch.setattr(orch, "SSHSession", FakeSSH)
    monkeypatch.setattr(
        orch,
        "HostAgent",
        lambda ssh: agents.append(FakeAgent(ssh, host_root, state["fail_app"])) or agents[-1],
    )
    monkeypatch.setattr(
        orch, "send_report", lambda to, subject, body: sent.append((to, subject, body))
    )
    monkeypatch.setattr(
        orch.RestoreHostBootstrap,
        "run",
        lambda self, major, lock_wait, volume_device=None: type(
            "R", (), {"notes": [f"bootstrapped yunohost {major}"]}
        )(),
    )
    return {
        "config": config,
        "client": fake_client,
        "provider": provider,
        "agents": agents,
        "sent": sent,
        "state": state,
        "paths": paths,
    }


# ----------------------------------------------------------------------- tests
def test_full_pipeline_sampled_run_passes_and_cleans_up(pipeline, capsys):
    cfg = pipeline["config"]
    run = orch.IntegrityRun(cfg, orch.RunOptions(mode="sampled", email=True))
    report = run.run()
    text = (cfg.paths.runs_dir / run.run_id / "report.txt").read_text()
    out = capsys.readouterr().out
    assert "[1/8] Inspecting Borg backup" in out and "[8/8] Cleaning up" in out

    assert report.fatal_error is None, report.fatal_error
    assert report.overall in ("PASS", "PASS WITH WARNINGS"), text
    assert report.backup_time == T_BACKUP + timedelta(minutes=2)
    ids = {c.id: c for c in report.components}
    assert set(ids) == {"conf_ldap", "conf_ynh_settings", "filebox", "data_mail"}
    assert ids["conf_ldap"].status == "PASS"
    filebox = ids["filebox"]
    assert filebox.status in ("PASS", "WARN") and filebox.data_kind in ("file", "media")
    assert (
        len(filebox.samples) == 10 and filebox.samples_readable == 8
    )  # broken.pdf empty + missing.pdf not extracted
    titles = {s.evidence.title for s in filebox.samples}
    assert any(t.startswith("IMG_") for t in titles)
    assert any(
        s.level == VerificationLevel.REFERENCED_BY_APPLICATION
        for s in filebox.samples
        if s.evidence.title.startswith("IMG_")
    )
    assert {c.name: c.status for c in filebox.checks}["HTTP health"] == "PASS"
    assert {c.name: c.status for c in filebox.checks}["Database restore"] == "PASS"
    mail = ids["data_mail"]
    assert mail.data_kind == "mail" and len(mail.samples) == 10
    assert all(s.level == VerificationLevel.VERIFIED_THROUGH_APPLICATION for s in mail.samples)
    assert any("Weekend plans" in s.evidence.title for s in mail.samples)
    assert "secret body" not in text and "pp-secret" not in text

    # Report structure: what needs attention first, then the summary, then the evidence.
    assert (
        text.index("NEEDS ATTENTION")
        < text.index("BACKUP MANIFEST COMPARISON")
        < text.index("APPLICATION: filebox")
    )
    assert "Recent backed-up objects" in text and "Weekend plans" in text and "IMG_" in text
    assert (
        "increased" in text or "+11" in text
    )  # previous generation was 10% smaller (stats-only baseline)
    assert "VERIFIED THROUGH APPLICATION" in text and "EXTRACTED AND READABLE" in text

    # The restore host is isolated before the system restore can bring back DynDNS keys, and
    # again after it, so the production domain can never be re-pointed at the test server.
    commands = [name for name, _ in pipeline["agents"][0].calls]
    assert commands.count("quarantine") == 2
    first_restore = commands.index("restore-core")
    assert commands.index("quarantine") < first_restore
    assert any(name == "quarantine" for name in commands[first_restore:]), (
        "no quarantine after the system restore"
    )

    # Borg-level checks ran on the production side, without the restore host.
    assert "check" in pipeline["client"].calls and any(
        c.startswith("dryrun:") for c in pipeline["client"].calls
    )
    assert report.borg_check.status == "PASS" and report.deep_check.status == "PASS"

    # Nothing failed, so no restore logs were kept.
    assert not (cfg.paths.runs_dir / run.run_id / "restore-logs").exists()

    # Manifest history persisted; cleanup verified through the provider; state finished.
    history = list((cfg.paths.history_dir).glob("*.json"))
    assert len(history) == 1
    assert pipeline["provider"].destroyed == ["srv-1"] and pipeline["provider"].live == {}
    state = RunStateStore(cfg.paths.runs_dir).load(run.run_id)
    assert state.status == "finished" and state.cleanup_status == "done" and state.server_id is None
    assert "all temporary resources destroyed" in report.cleanup_status
    # Email sent with the same content.
    assert (
        pipeline["sent"]
        and pipeline["sent"][0][0] == "root"
        and "BACKUP MANIFEST COMPARISON" in pipeline["sent"][0][2]
    )
    # Safety: the borg app is never restored; skeleton dirs were requested for the sparse app restore.
    calls = pipeline["agents"][0].calls
    core_specs = [s for c, s in calls if c == "restore-core"]
    assert all("borg" not in s["targets"]["apps"] for s in core_specs)
    app_spec = next(s for s in core_specs if s["targets"]["apps"] == ["filebox"])
    assert (
        app_spec["large_roots"][0]["archive_path"]
        == "apps/filebox/backup/home/yunohost.app/filebox"
    )
    assert len(app_spec["large_roots"][0]["skeleton"]) >= 3
    system_spec = next(s for s in core_specs if s["targets"]["system"])
    assert "conf_manually_modified_files" not in system_spec["targets"]["system"]
    quarantine = next(a for c, a in calls if c == "quarantine")
    assert quarantine is None  # args-only call, spec None


def test_fatal_error_keeps_the_host_and_records_where_the_run_died(pipeline, monkeypatch):
    """A dropped SSH connection is exactly when the restore host is worth keeping for a look."""
    from borg_backup_integrity_check.errors import RestoreHostError

    cfg = pipeline["config"]
    original = FakeAgent.call

    def call(self, command, spec=None, args=None, timeout=0):
        if command == "domains":
            raise RestoreHostError(
                "ssh connection to 203.0.113.7:22022 failed: "
                "kex_exchange_identification: read: Connection reset by peer"
            )
        return original(self, command, spec=spec, args=args, timeout=timeout)

    monkeypatch.setattr(FakeAgent, "call", call)
    run = orch.IntegrityRun(cfg, orch.RunOptions(mode="sampled", keep_host_on_failure=True))
    report = run.run()

    assert report.overall == "FAIL"
    assert "kex_exchange_identification" in report.fatal_error
    assert pipeline["provider"].destroyed == [] and "srv-1" in pipeline["provider"].live
    assert report.retained_host is not None and report.retained_host.ssh_port == 22022
    assert "retained" in report.cleanup_status
    state = RunStateStore(cfg.paths.runs_dir).load(run.run_id)
    assert state.status == "retained" and state.server_id == "srv-1"
    assert state.error == report.fatal_error
    assert state.failed_phase == "restoring"  # not "cleaning", which `phase` has moved on to


def test_an_interrupted_run_is_still_cleaned_up(pipeline, monkeypatch):
    cfg = pipeline["config"]
    run = orch.IntegrityRun(cfg, orch.RunOptions(mode="sampled", keep_host_on_failure=True))
    original = FakeAgent.call

    def call(self, command, spec=None, args=None, timeout=0):
        if command == "restore-core" and spec["targets"]["system"]:
            run._interrupted = True
        return original(self, command, spec=spec, args=args, timeout=timeout)

    monkeypatch.setattr(FakeAgent, "call", call)
    report = run.run()

    assert "interrupted" in report.fatal_error
    assert report.retained_host is None
    assert pipeline["provider"].destroyed == ["srv-1"]
    state = RunStateStore(cfg.paths.runs_dir).load(run.run_id)
    assert state.status == "interrupted" and state.failed_phase == "restoring"


def test_pipeline_second_run_compares_with_history_and_flags_missing(pipeline):
    cfg = pipeline["config"]
    first = orch.IntegrityRun(cfg, orch.RunOptions(mode="sampled", inspect_only=True)).run()
    assert first.comparison.previous.stats_only
    # Simulate a newer backup where the filebox archive disappeared and mail shrank a lot.
    client = pipeline["client"]
    original = client.list_archives

    def newer():
        refs = [
            r for r in original() if not (r.name.startswith("auto_filebox") and r.start >= T_BACKUP)
        ]
        return refs + [
            ArchiveRef(f"auto_conf-{T_NEWER:%Y-%m-%dT%H:%M:%S}", "cN", T_NEWER),
            ArchiveRef(
                f"auto_data-{T_NEWER + timedelta(minutes=1):%Y-%m-%dT%H:%M:%S}",
                "dN",
                T_NEWER + timedelta(minutes=1),
            ),
        ]

    client.list_archives = newer
    client.listings["auto_data"] = client.listings["auto_data"][:20]
    second = orch.IntegrityRun(cfg, orch.RunOptions(mode="sampled", inspect_only=True)).run()
    messages = [a.message for a in second.comparison.anomalies]
    assert any("auto_filebox" in m and "missing" in m for m in messages)
    assert second.overall == "FAIL"  # missing archive is an error
    assert any("auto_filebox" in w for w in second.warnings)  # stale component warning
    assert second.cleanup_status.startswith("not needed")


def test_pipeline_core_restore_failure_is_reported_and_still_cleans_up(pipeline):
    pipeline["state"]["fail_app"] = "filebox"
    cfg = pipeline["config"]
    run = orch.IntegrityRun(cfg, orch.RunOptions(mode="sampled"))
    report = run.run()
    filebox = next(c for c in report.components if c.id == "filebox")
    assert filebox.status == "FAIL" and report.overall == "FAIL" and report.core_failures == 1
    assert filebox.samples == [] and "samples skipped" in " ".join(filebox.notes)
    text = (cfg.paths.runs_dir / run.run_id / "report.txt").read_text()
    assert "WHAT FAILED" in text
    assert "filebox" in text and "core restore" in text and "app restore script failed" in text
    assert text.index("WHAT FAILED") < text.index("RUN SUMMARY")
    assert pipeline["provider"].live == {}
    assert RunStateStore(cfg.paths.runs_dir).load(run.run_id).status == "failed"

    # The restore host is gone; its account of the failure has to outlive it.
    saved = cfg.paths.runs_dir / run.run_id / "restore-logs" / "filebox.log"
    assert filebox.saved_log == str(saved)
    kept = saved.read_text()
    assert "mysql: Access denied for user" in kept  # the operation log itself
    assert "app restore script failed" in kept  # and the restore output it was summarised from
    assert "20260914-201242-backup_restore_app.yml" in kept  # where it came from
    assert str(saved) in text and "WHY A RESTORE FAILED" in text


def test_pipeline_retain_keeps_host_and_reports_access(pipeline):
    cfg = pipeline["config"]
    run = orch.IntegrityRun(cfg, orch.RunOptions(mode="sampled", retain_hours=2))
    report = run.run()
    assert (
        report.retained_host is not None
        and report.retained_host.address == "198.51.100.5"
        and report.retained_host.ssh_port == 22022
    )
    assert pipeline["provider"].live == {"srv-1": f"bbic-{run.run_id}"}
    state = RunStateStore(cfg.paths.runs_dir).load(run.run_id)
    assert state.status == "retained" and state.retained_until is not None and state.needs_cleanup
    text = (cfg.paths.runs_dir / run.run_id / "report.txt").read_text()
    assert (
        "RETAINED RESTORE SERVER" in text
        and "ssh -p 22022 root@198.51.100.5" in text
        and "example.org" in text
    )


def test_pipeline_cleanup_failure_is_prominent(pipeline, monkeypatch):
    cfg = pipeline["config"]
    provider = pipeline["provider"]

    def boom(vm_id):
        from borg_backup_integrity_check.errors import ProviderError

        raise ProviderError("API down")

    provider.destroy_vm = boom
    run = orch.IntegrityRun(cfg, orch.RunOptions(mode="sampled"))
    report = run.run()
    assert report.overall == "FAIL" and "API down" in report.cleanup_error
    text = (cfg.paths.runs_dir / run.run_id / "report.txt").read_text()
    assert "OVERALL: FAIL" in text
    assert "temporary resources may still exist" in text and "API down" in text
    state = RunStateStore(cfg.paths.runs_dir).load(run.run_id)
    assert state.cleanup_status == "failed" and state.server_id == "srv-1"


def test_out_of_scope_apps_skip_full_listing(pipeline):
    """Scoped run: only selected apps are fully listed; others get manifest metrics from borg info."""
    cfg = pipeline["config"]
    run = orch.IntegrityRun(
        cfg,
        orch.RunOptions(mode="sampled", inspect_only=True, components=["conf_ldap", "data_mail"]),
    )
    report = run.run()
    # filebox is an app not selected -> its archive listing is skipped.
    listed_archives = set(run.listings)
    assert not any("filebox" in name for name in listed_archives)
    assert any("auto_data" in name for name in listed_archives)  # system data always listed
    filebox = next(c for c in run.components if c.id == "filebox")
    assert filebox.listed is False and filebox.large_roots == []
    # Manifest still covers filebox, with size/count taken from borg info (fake client stats).
    metrics = report.manifest.components["filebox"]
    assert metrics.logical_size > 0 and metrics.file_count > 0 and metrics.large_roots == []
    assert "borg info" in " ".join(filebox.notes)

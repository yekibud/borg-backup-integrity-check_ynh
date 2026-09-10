from datetime import datetime
from pathlib import Path

import pytest

from borg_backup_integrity_check.config import DEFAULTS, AppConfig, Paths
from borg_backup_integrity_check.errors import ConfigurationError
from borg_backup_integrity_check.restore.cloud_init import build_user_data
from borg_backup_integrity_check.restore.planner import build_plan
from borg_backup_integrity_check.run.ids import host_name_for, is_run_id, new_run_id
from borg_backup_integrity_check.run.state import RunState, RunStateStore
from borg_backup_integrity_check.secrets import SecretStore
from borg_backup_integrity_check.yunohost import BorgYnhInstance, discover_borg_ynh


def _paths(tmp_path: Path) -> Paths:
    p = Paths(app_id="borg-backup-integrity-check")
    p.settings_file = tmp_path / "settings.yml"
    p.etc_dir = tmp_path / "etc"
    p.data_dir = tmp_path / "data"
    p.log_dir = tmp_path / "log"
    p.install_dir = tmp_path / "www"
    return p


def _config(tmp_path: Path, **overrides) -> AppConfig:
    paths = _paths(tmp_path)
    settings = {
        "id": "borg-backup-integrity-check",
        "cloud_provider": "hetzner",
        "use_borg_ynh": "1",
    }
    settings.update(overrides)
    import yaml

    paths.settings_file.write_text(yaml.safe_dump(settings))
    return AppConfig.load(paths)


def test_secret_store_permissions_and_status(tmp_path):
    store = SecretStore(tmp_path / "etc" / "secrets.json")
    assert not store.is_configured("hetzner_token")
    store.set("hetzner_token", "x" * 64)
    assert oct(store.path.stat().st_mode & 0o777) == "0o600"
    assert oct(store.path.parent.stat().st_mode & 0o777) == "0o700"
    assert store.get("hetzner_token") == "x" * 64
    status = store.status("hetzner_token")
    assert (
        status["configured"]
        and len(status["fingerprint"]) == 8
        and "x" not in status["fingerprint"] * 0 + "y"
    )
    with pytest.raises(ValueError):
        store.set("hetzner_token", "")
    assert store.delete("hetzner_token") and not store.delete("hetzner_token")


def test_config_defaults_typing_and_on_calendar(tmp_path):
    cfg = _config(
        tmp_path,
        sample_size="25",
        schedule_enabled="0",
        warn_growth_pct="",
        schedule_frequency="weekly",
        schedule_weekday="Fri",
        schedule_time="07:30",
    )
    assert cfg.sample_size == 25 and isinstance(cfg.sample_size, int)
    assert cfg.schedule_enabled is False
    assert cfg.warn_growth_pct == DEFAULTS["warn_growth_pct"]
    assert cfg.on_calendar() == "Fri *-*-* 07:30:00"
    cfg2 = _config(tmp_path, schedule_frequency="monthly")
    assert cfg2.on_calendar() == "*-*-01 09:00:00"
    assert _config(tmp_path).on_calendar() == "*-*-* 09:00:00"
    assert cfg.components_list == ["all"]
    assert "borg" in cfg.never_restore and "borg-backup-integrity-check" in cfg.never_restore
    assert cfg.report_recipient() == "root"
    assert _config(tmp_path, report_email="ops@example.org").report_recipient() == "ops@example.org"
    assert cfg.provider_settings["hetzner_location"] == "fsn1"


def test_borg_source_reuses_borg_ynh_or_manual(tmp_path, monkeypatch):
    monkeypatch.setenv("BBIC_BORG_BINARY", "/bin/true")
    key = tmp_path / "id_borg_ed25519"
    key.write_text("key")
    inst = BorgYnhInstance(
        app="borg",
        repository="ssh://u@h:23/./backup",
        passphrase="pp",
        remote_path=None,
        install_dir=str(tmp_path),
        ssh_key_path=str(key),
        binary=None,
    )
    cfg = _config(tmp_path)
    (cfg.paths.keys_dir).mkdir(parents=True)
    src = cfg.borg_source(discovery=[inst])
    assert (
        src.repository == "ssh://u@h:23/./backup"
        and src.passphrase == "pp"
        and src.origin == "borg_ynh:borg"
    )
    assert src.ssh_key_path == str(key) and src.remote_repository == src.repository
    with pytest.raises(ConfigurationError):
        cfg.borg_source(discovery=[])
    manual = _config(
        tmp_path,
        use_borg_ynh="0",
        borg_repository="ssh://x@y/./r",
        borg_repository_remote="ssh://x@10.0.0.1/./r",
    )
    manual.secrets.set("borg_passphrase", "manual-pass")
    src = manual.borg_source(discovery=[])
    assert (
        src.origin == "manual"
        and src.passphrase == "manual-pass"
        and src.remote_repository == "ssh://x@10.0.0.1/./r"
    )
    with pytest.raises(ConfigurationError):
        _config(tmp_path, use_borg_ynh="0", borg_repository="").borg_source(discovery=[])


def test_discover_borg_ynh_from_settings_dir(tmp_path):
    apps = tmp_path / "apps"
    (apps / "borg" / "scripts").mkdir(parents=True)
    (apps / "borg" / "settings.yml").write_text(
        "id: borg\nrepository: ssh://u@h/./backup\npassphrase: secret-pp\ninstall_dir: /var/www/borg\nremote_path: ''\n"
    )
    (apps / "borg" / "manifest.toml").write_text('id = "borg"\npackaging_format = 2\n')
    (apps / "nextcloud").mkdir()
    (apps / "nextcloud" / "settings.yml").write_text("id: nextcloud\n")
    ssh = tmp_path / "ssh"
    ssh.mkdir()
    (ssh / "id_borg_ed25519").write_text("k")
    found = discover_borg_ynh(apps, ssh)
    assert (
        len(found) == 1
        and found[0].app == "borg"
        and found[0].passphrase == "secret-pp"
        and found[0].ssh_key_path == str(ssh / "id_borg_ed25519")
    )
    from borg_backup_integrity_check.redaction import redact

    assert "secret-pp" not in redact("passphrase secret-pp leaked")


def test_run_state_store_roundtrip_and_unfinished(tmp_path):
    store = RunStateStore(tmp_path / "runs")
    rid = new_run_id(datetime(2026, 9, 10, 9, 0))
    assert is_run_id(rid) and host_name_for(rid).startswith("bbic-20260910-090000-")
    state = RunState(
        run_id=rid,
        mode="sampled",
        provider="hetzner",
        started_at=datetime(2026, 9, 10, 9, 0),
        server_id="42",
        volume_ids=["9"],
    )
    store.save(state)
    assert oct((tmp_path / "runs" / rid / "state.json").stat().st_mode & 0o777) == "0o600"
    loaded = store.load(rid)
    assert loaded.server_id == "42" and loaded.needs_cleanup and loaded.has_cloud_resources
    assert [s.run_id for s in store.unfinished()] == [rid]
    loaded.server_id = None
    loaded.volume_ids = []
    loaded.cleanup_status = "done"
    loaded.status = "finished"
    store.save(loaded)
    assert store.unfinished() == []
    for i in range(5):
        store.save(
            RunState(
                run_id=f"2026090{i}-000000-aaaa",
                mode="sampled",
                provider="hetzner",
                started_at=datetime(2026, 9, i + 1),
                status="finished",
                cleanup_status="not_needed",
            )
        )
    assert store.prune(keep=2) == 4
    assert len(store.list()) == 2


def test_cloud_init_user_data_contains_maintenance_sshd():
    data = build_user_data("bbic-x", "ssh-ed25519 AAAA test", 22022, "run1")
    assert data.startswith("#cloud-config\n")
    assert "Port 22022" in data and "bbic-sshd.service" in data and "ssh-ed25519 AAAA test" in data
    assert "PasswordAuthentication no" in data and "PermitRootLogin prohibit-password" in data
    import json

    doc = json.loads(data.split("\n", 1)[1])
    assert doc["disable_root"] is False and doc["runcmd"][-1][-1] == "bbic-sshd.service"
    assert len(data.encode()) < 32 * 1024


def test_planner_orders_and_sizes(synthetic_app_listing, synthetic_app_layout, archive_ref):
    from borg_backup_integrity_check.borg.listing import DirectoryAggregates
    from borg_backup_integrity_check.discovery.components import components_from_layout
    from borg_backup_integrity_check.discovery.large_data import LargeDataDiscovery
    from borg_backup_integrity_check.discovery.profiles import ProfileRegistry
    from borg_backup_integrity_check.sampling.sampler import GenericSampler, SamplingRules

    synthetic_app_layout.info.system = {
        "conf_ynh_settings": {},
        "conf_ldap": {},
        "conf_manually_modified_files": {},
        "data_mail": {},
    }
    comps = components_from_layout(archive_ref, "auto_filebox", synthetic_app_layout)
    comps += [
        c
        for c in components_from_layout(archive_ref, "auto_conf", synthetic_app_layout)
        if not c.is_app
    ]
    agg = DirectoryAggregates.build(synthetic_app_listing)
    comps = [LargeDataDiscovery(ProfileRegistry([])).discover(c, agg) for c in comps]
    from borg_backup_integrity_check.borg.layout import AppArchiveEntry

    comps.append(
        type(comps[0])(
            id="borg",
            kind="app",
            archive=archive_ref,
            archive_component="auto_borg",
            root="apps/borg",
            layout=synthetic_app_layout,
            app=AppArchiveEntry("borg", {"id": "borg"}, {"id": "borg"}),
        )
    )
    samples = {
        "filebox": GenericSampler(SamplingRules(sample_size=20)).select(
            comps[0], synthetic_app_listing
        )
    }
    plan = build_plan(comps, samples, "sampled", ["all"], {"borg", "borg-backup-integrity-check"})
    assert [p.component.id for p in plan.system_conf] == ["conf_ldap", "conf_ynh_settings"]
    assert [p.component.id for p in plan.apps] == ["filebox"]
    assert [p.component.id for p in plan.system_data] == ["data_mail"]
    assert ("conf_manually_modified_files", "skipped by policy") in plan.skipped
    assert any(s[0] == "borg" for s in plan.skipped)
    assert plan.apps[0].payload_mode == "sampled" and plan.system_data[0].restore_core is False
    assert plan.required_disk_gb() >= 8 + 3
    full = build_plan(comps, samples, "full", ["filebox"], set())
    assert full.apps[0].payload_mode == "full"
    full_all = build_plan(comps, samples, "full", ["all"], {"borg"})
    assert full_all.disk_estimate_bytes() > plan.disk_estimate_bytes()
    assert [p.component.id for p in full.system_conf] == []  # not selected
    assert "estimated disk need" in full.summary()

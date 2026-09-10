from datetime import datetime, timedelta

import pytest

from borg_backup_integrity_check.errors import CleanupError, ProviderError
from borg_backup_integrity_check.providers.base import (
    LABEL_MANAGED,
    LABEL_OWNER,
    LABEL_RUN,
    MANAGED_BY,
    ManagedResource,
)
from borg_backup_integrity_check.providers.static import StaticHostProvider
from borg_backup_integrity_check.run.cleanup import LifecycleManager
from borg_backup_integrity_check.run.state import RunState, RunStateStore

NOW = datetime(2026, 9, 10, 12, 0)


class FakeProvider(StaticHostProvider):
    name = "fake"

    def __init__(self):
        super().__init__("127.0.0.1")
        self.servers = {"42": "bbic-old", "43": "bbic-retained", "44": "bbic-orphan"}
        self.volumes = {"9": "42"}
        self.fail_volume = False

    def destroy_vm(self, vm_id):
        self.servers.pop(vm_id, None)

    def get_vm(self, vm_id):
        return None if vm_id not in self.servers else super().get_vm(vm_id)

    def destroy_volume(self, volume_id):
        if self.fail_volume:
            raise ProviderError("volume busy")
        self.volumes.pop(volume_id, None)

    def list_managed_resources(self, owner=None):
        res = []
        for sid, name in self.servers.items():  # noqa: B007
            run = {"42": "20260909-090000-aaaa", "43": "20260910-090000-bbbb", "44": None}[sid]
            res.append(
                ManagedResource(
                    "server",
                    sid,
                    name,
                    run,
                    "owner1",
                    NOW - timedelta(days=2),
                    {
                        "labels": {
                            LABEL_MANAGED: MANAGED_BY,
                            LABEL_OWNER: "owner1",
                            LABEL_RUN: run or "",
                        }
                    },
                )
            )
        for vid, _sid in self.volumes.items():
            res.append(
                ManagedResource(
                    "volume",
                    vid,
                    "bbic-vol",
                    "20260909-090000-aaaa",
                    "owner1",
                    NOW - timedelta(days=2),
                    {},
                )
            )
        return res


def _store(tmp_path):
    store = RunStateStore(tmp_path / "runs")
    old = RunState(
        run_id="20260909-090000-aaaa",
        mode="sampled",
        provider="fake",
        started_at=NOW - timedelta(days=1),
        status="interrupted",
        server_id="42",
        volume_ids=["9"],
    )
    retained = RunState(
        run_id="20260910-090000-bbbb",
        mode="sampled",
        provider="fake",
        started_at=NOW - timedelta(hours=3),
        status="retained",
        server_id="43",
        retained_until=NOW + timedelta(hours=1),
    )
    store.save(old)
    store.save(retained)
    return store


def test_destroy_run_and_failure_reporting(tmp_path):
    provider, store = FakeProvider(), _store(tmp_path)
    manager = LifecycleManager(provider, store, "owner1")
    state = store.load("20260909-090000-aaaa")
    report = manager.destroy_run(state)
    assert report.ok and "42" not in provider.servers and "9" not in provider.volumes
    assert store.load(state.run_id).cleanup_status == "done"
    provider.fail_volume = True
    state2 = RunState(
        run_id="20260910-100000-cccc",
        mode="sampled",
        provider="fake",
        started_at=NOW,
        server_id="43",
        volume_ids=["9"],
    )
    provider.volumes["9"] = "43"
    store.save(state2)
    with pytest.raises(CleanupError):
        manager.destroy_run(state2)
    saved = store.load(state2.run_id)
    assert (
        saved.cleanup_status == "failed"
        and "volume busy" in saved.cleanup_error
        and saved.server_id is None
        and saved.volume_ids == ["9"]
    )


def test_find_stale_respects_retained_and_running_runs(tmp_path):
    provider, store = FakeProvider(), _store(tmp_path)
    manager = LifecycleManager(provider, store, "owner1")
    stale = manager.find_stale(now=NOW)
    ids = {(r.kind, r.id) for r in stale}
    assert ("server", "42") in ids and ("volume", "9") in ids and ("server", "44") in ids
    assert ("server", "43") not in ids  # retained and not expired
    report = manager.destroy_resources(stale)
    assert report.ok and provider.servers == {"43": "bbic-retained"} and provider.volumes == {}
    assert store.load("20260909-090000-aaaa").cleanup_status == "done"


def test_expire_retained(tmp_path):
    provider, store = FakeProvider(), _store(tmp_path)
    manager = LifecycleManager(provider, store, "owner1")
    assert manager.expire_retained(now=NOW) == []
    reports = manager.expire_retained(now=NOW + timedelta(hours=2))
    assert len(reports) == 1 and "43" not in provider.servers
    assert store.load("20260910-090000-bbbb").status == "finished"
    assert manager.verify_gone(store.load("20260910-090000-bbbb")) == []

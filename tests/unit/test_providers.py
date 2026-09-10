"""Provider contract tests: both backends must behave identically through the neutral interface."""

from __future__ import annotations

import json
from typing import Any

import pytest

from borg_backup_integrity_check.errors import ConfigurationError, ProviderError
from borg_backup_integrity_check.providers.base import (
    LABEL_MANAGED,
    LABEL_OWNER,
    LABEL_RUN,
    MANAGED_BY,
    VMSpec,
    labels_to_tags,
    run_labels,
    tags_to_labels,
)
from borg_backup_integrity_check.providers.digitalocean import DigitalOceanProvider
from borg_backup_integrity_check.providers.hetzner import HetznerCloudProvider
from borg_backup_integrity_check.providers.registry import create_provider
from borg_backup_integrity_check.providers.static import StaticHostProvider


class FakeApi:
    """Records requests and serves canned JSON responses (routes keyed by METHOD path)."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method, path, *, params=None, json=None, ok=(200, 201, 202, 204)):
        self.calls.append((method, path, json if json is not None else params))
        key = f"{method} {path}"
        if key not in self.routes:
            raise ProviderError(f"{key}: HTTP 404: not found", status=404)
        value = self.routes[key]
        return value(params, json) if callable(value) else value

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def paginate(self, path, key, params=None, per_page=50, **kw):
        data = self.request("GET", path, params=params) or {}
        return data.get(key, [])


LABELS = run_labels("20260910-090000-ab12", "owner1")


def hetzner_provider():
    servers: dict[int, dict] = {}
    routes = {
        "GET /servers": lambda p, j: {
            "servers": [
                s
                for s in servers.values()
                if not p or "label_selector" not in p or MANAGED_BY in p["label_selector"]
            ],
            "meta": {"pagination": {"total_entries": len(servers)}},
        },
        "GET /locations": {
            "locations": [
                {
                    "name": "fsn1",
                    "city": "Falkenstein",
                    "description": "DC Park 1",
                    "country": "DE",
                },
                {"name": "nbg1", "city": "Nuremberg", "description": "DC 3", "country": "DE"},
            ]
        },
        "GET /server_types": {
            "server_types": [
                {
                    "name": "cx22",
                    "cores": 2,
                    "memory": 4.0,
                    "disk": 40,
                    "architecture": "x86",
                    "cpu_type": "shared",
                    "deprecation": None,
                    "locations": [{"name": "fsn1"}, {"name": "nbg1"}],
                    "prices": [
                        {
                            "location": "fsn1",
                            "price_hourly": {"gross": "0.0063"},
                            "price_monthly": {"gross": "3.79"},
                        }
                    ],
                },
                {
                    "name": "cx32",
                    "cores": 4,
                    "memory": 8.0,
                    "disk": 80,
                    "architecture": "x86",
                    "cpu_type": "shared",
                    "deprecation": None,
                    "locations": [{"name": "fsn1"}],
                    "prices": [
                        {
                            "location": "fsn1",
                            "price_hourly": {"gross": "0.0113"},
                            "price_monthly": {"gross": "6.80"},
                        }
                    ],
                },
                {
                    "name": "cax11",
                    "cores": 2,
                    "memory": 4.0,
                    "disk": 40,
                    "architecture": "arm",
                    "cpu_type": "shared",
                    "deprecation": None,
                    "locations": [{"name": "fsn1"}],
                    "prices": [
                        {
                            "location": "fsn1",
                            "price_hourly": {"gross": "0.0055"},
                            "price_monthly": {"gross": "3.29"},
                        }
                    ],
                },
                {
                    "name": "cx11",
                    "cores": 1,
                    "memory": 2.0,
                    "disk": 20,
                    "architecture": "x86",
                    "cpu_type": "shared",
                    "deprecation": {"announced": "2024-01-01", "unavailable_after": "2024-06-01"},
                    "locations": [{"name": "fsn1"}],
                    "prices": [],
                },
            ]
        },
        "GET /images": {
            "images": [
                {
                    "id": 1,
                    "name": "debian-11",
                    "os_flavor": "debian",
                    "os_version": "11",
                    "type": "system",
                    "status": "available",
                },
                {
                    "id": 2,
                    "name": "debian-12",
                    "os_flavor": "debian",
                    "os_version": "12",
                    "type": "system",
                    "status": "available",
                },
                {"id": 3, "name": "ubuntu-24.04", "os_flavor": "ubuntu", "os_version": "24.04"},
            ]
        },
        "GET /ssh_keys": lambda p, j: {"ssh_keys": []},
        "POST /ssh_keys": lambda p, j: {
            "ssh_key": {"id": 77, "name": j["name"], "fingerprint": "aa:bb"}
        },
        "POST /servers": lambda p, j: (
            servers.__setitem__(
                42,
                {
                    "id": 42,
                    "name": j["name"],
                    "status": "initializing",
                    "public_net": {"ipv4": {"ip": "203.0.113.10"}, "ipv6": {"ip": "2001:db8::/64"}},
                    "datacenter": {"location": {"name": j["location"]}},
                    "server_type": {"name": j["server_type"]},
                    "labels": j["labels"],
                    "created": "2026-09-10T09:00:00+00:00",
                    "volumes": [],
                },
            )
            or {"server": servers[42], "action": {"id": 1}}
        ),
        "GET /servers/42": lambda p, j: (
            {"server": dict(servers[42], status="running")}
            if 42 in servers
            else (_ for _ in ()).throw(ProviderError("x", status=404))
        ),
        "DELETE /servers/42": lambda p, j: (
            (servers.pop(42, None) and {"action": {"id": 2}}) or {"action": {"id": 2}}
        ),
        "GET /actions/1": {"action": {"id": 1, "status": "success"}},
        "GET /actions/2": {"action": {"id": 2, "status": "success"}},
        "GET /actions/3": {"action": {"id": 3, "status": "success"}},
        "POST /volumes": lambda p, j: {
            "volume": {
                "id": 9,
                "name": j["name"],
                "size": j["size"],
                "status": "available",
                "server": j.get("server"),
                "linux_device": "/dev/disk/by-id/scsi-0HC_Volume_9",
                "labels": j["labels"],
                "created": "2026-09-10T09:01:00+00:00",
            },
            "action": {"id": 3},
            "next_actions": [],
        },
        "GET /volumes/9": {
            "volume": {
                "id": 9,
                "name": "bbic-vol",
                "size": 100,
                "status": "available",
                "server": 42,
                "labels": LABELS,
            }
        },
        "POST /volumes/9/actions/detach": {"action": {"id": 3}},
        "DELETE /volumes/9": None,
        "GET /volumes": {
            "volumes": [
                {
                    "id": 9,
                    "name": "bbic-vol",
                    "size": 100,
                    "status": "available",
                    "labels": LABELS,
                    "created": "2026-09-10T09:01:00+00:00",
                }
            ]
        },
        "DELETE /ssh_keys/77": None,
    }
    api = FakeApi(routes)
    return HetznerCloudProvider("x" * 64, api=api), api


def digitalocean_provider():
    droplets: dict[int, dict] = {}
    tags = labels_to_tags(LABELS)
    routes = {
        "GET /account": {
            "account": {"email": "me@example.org", "status": "active", "droplet_limit": 25}
        },
        "GET /regions": {
            "regions": [
                {
                    "slug": "fra1",
                    "name": "Frankfurt 1",
                    "available": True,
                    "sizes": ["s-2vcpu-4gb"],
                },
                {"slug": "nyc1", "name": "New York 1", "available": False, "sizes": []},
            ]
        },
        "GET /sizes": {
            "sizes": [
                {
                    "slug": "s-1vcpu-1gb",
                    "memory": 1024,
                    "vcpus": 1,
                    "disk": 25,
                    "price_hourly": 0.00893,
                    "price_monthly": 6.0,
                    "regions": ["fra1", "nyc1"],
                    "available": True,
                    "description": "Basic",
                },
                {
                    "slug": "s-2vcpu-4gb",
                    "memory": 4096,
                    "vcpus": 2,
                    "disk": 80,
                    "price_hourly": 0.03571,
                    "price_monthly": 24.0,
                    "regions": ["fra1"],
                    "available": True,
                    "description": "Basic",
                },
                {
                    "slug": "s-4vcpu-8gb",
                    "memory": 8192,
                    "vcpus": 4,
                    "disk": 160,
                    "price_hourly": 0.07143,
                    "price_monthly": 48.0,
                    "regions": ["fra1"],
                    "available": True,
                    "description": "Basic",
                },
            ]
        },
        "GET /images": {
            "images": [
                {
                    "id": 1,
                    "slug": "debian-11-x64",
                    "distribution": "Debian",
                    "name": "11 x64",
                    "status": "available",
                },
                {
                    "id": 2,
                    "slug": "debian-12-x64",
                    "distribution": "Debian",
                    "name": "12 x64",
                    "status": "available",
                },
            ]
        },
        "GET /account/keys": {"ssh_keys": []},
        "POST /account/keys": lambda p, j: {
            "ssh_key": {
                "id": 55,
                "name": j["name"],
                "fingerprint": "cc:dd",
                "public_key": j["public_key"],
            }
        },
        "GET /projects": {"projects": [{"id": "proj-uuid", "name": "Backups"}]},
        "POST /droplets": lambda p, j: (
            droplets.__setitem__(
                101,
                {
                    "id": 101,
                    "name": j["name"],
                    "status": "new",
                    "networks": {"v4": [], "v6": []},
                    "region": {"slug": j["region"]},
                    "size_slug": j["size"],
                    "tags": j["tags"],
                    "created_at": "2026-09-10T09:00:00Z",
                    "volume_ids": [],
                },
            )
            or {"droplet": droplets[101]}
        ),
        "GET /droplets/101": lambda p, j: (
            {
                "droplet": dict(
                    droplets[101],
                    status="active",
                    networks={
                        "v4": [
                            {"type": "public", "ip_address": "198.51.100.7"},
                            {"type": "private", "ip_address": "10.0.0.2"},
                        ],
                        "v6": [{"type": "public", "ip_address": "2001:db8::7"}],
                    },
                )
            }
            if 101 in droplets
            else (_ for _ in ()).throw(ProviderError("x", status=404))
        ),
        "DELETE /droplets/101": lambda p, j: droplets.pop(101, None) and None,
        "GET /droplets": lambda p, j: {
            "droplets": [d for d in droplets.values() if p and p.get("tag_name") in d["tags"]]
        },
        "POST /volumes": lambda p, j: {
            "volume": {
                "id": "vol-1",
                "name": j["name"],
                "size_gigabytes": j["size_gigabytes"],
                "droplet_ids": [],
                "tags": j["tags"],
                "created_at": "2026-09-10T09:01:00Z",
            }
        },
        "POST /volumes/actions": lambda p, j: {
            "action": {"id": 500, "status": "in-progress", "type": j["type"]}
        },
        "GET /actions/500": {"action": {"id": 500, "status": "completed"}},
        "GET /volumes/vol-1": {
            "volume": {
                "id": "vol-1",
                "name": "bbic-vol",
                "size_gigabytes": 100,
                "droplet_ids": [101],
                "tags": tags,
            }
        },
        "POST /volumes/vol-1/actions": lambda p, j: {
            "action": {"id": 500, "status": "in-progress"}
        },
        "DELETE /volumes/vol-1": None,
        "GET /volumes": {
            "volumes": [
                {
                    "id": "vol-1",
                    "name": "bbic-vol",
                    "size_gigabytes": 100,
                    "droplet_ids": [],
                    "tags": tags,
                    "created_at": "2026-09-10T09:01:00Z",
                },
                {
                    "id": "other",
                    "name": "keep-me",
                    "size_gigabytes": 10,
                    "droplet_ids": [],
                    "tags": ["prod"],
                },
            ]
        },
        "DELETE /account/keys/55": None,
    }
    api = FakeApi(routes)
    return DigitalOceanProvider("dop_v1_" + "0" * 64, project="Backups", api=api), api


PROVIDERS = {
    "hetzner": (hetzner_provider, "fsn1", "203.0.113.10"),
    "digitalocean": (digitalocean_provider, "fra1", "198.51.100.7"),
}


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_provider_contract_lifecycle(name):
    factory, region, expected_ip = PROVIDERS[name]
    provider, api = factory()
    assert provider.name == name
    assert (
        "reachable" in provider.validate_credentials()
        or "account" in provider.validate_credentials()
    )

    regions = provider.list_regions()
    assert any(r.id == region for r in regions)

    sizes = provider.list_sizes(region)
    assert all(s.memory_mb > 0 and s.disk_gb > 0 for s in sizes)
    chosen = provider.choose_size(region, min_memory_mb=4000, min_disk_gb=40)
    assert chosen.memory_mb >= 4000 and chosen.disk_gb >= 40 and chosen.architecture == "x86"
    assert chosen.id in ("cx22", "s-2vcpu-4gb")
    with pytest.raises(LookupError):
        provider.choose_size(region, min_memory_mb=4000, min_disk_gb=40, preferred="does-not-exist")
    with pytest.raises(LookupError):
        provider.choose_size(region, min_memory_mb=1024 * 1024, min_disk_gb=1)

    image = provider.find_image("debian", "12")
    assert image.version == "12" and image.family == "debian"
    with pytest.raises(ProviderError):
        provider.find_image("debian", "9")

    key = provider.ensure_ssh_key(
        "bbic-owner1",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBf4IlMZd2P1MJzUwQSpDAqDNQJKpPRdchhWExMrkslI test",
        LABELS,
    )
    assert key.id

    spec = VMSpec(
        name="bbic-20260910-090000-ab12",
        region=region,
        size=chosen.id,
        image=image,
        ssh_keys=[key],
        user_data="#cloud-config\n",
        labels=LABELS,
    )
    vm = provider.create_vm(spec)
    assert vm.id and vm.name == spec.name
    running = provider.wait_for_vm(vm.id, timeout=5)
    assert running.status == "running" and running.address == expected_ip
    assert running.labels[LABEL_RUN] == LABELS[LABEL_RUN]
    assert running.labels[LABEL_MANAGED] == MANAGED_BY

    if provider.capabilities.volumes:
        volume = provider.create_volume("bbic-vol", 100, region, LABELS, attach_to=vm.id)
        assert volume.size_gb == 100
        provider.destroy_volume(volume.id)

    managed = provider.list_managed_resources(owner="owner1")
    kinds = {m.kind for m in managed}
    assert "server" in kinds
    assert all(m.owner == "owner1" for m in managed)
    assert not any(m.name == "keep-me" for m in managed)

    provider.destroy_vm(vm.id)
    assert provider.get_vm(vm.id) is None
    provider.destroy_vm(vm.id)  # idempotent
    provider.delete_ssh_key(key)

    # Credentials must never appear in request bodies we send (they live in headers).
    for _, _, payload in api.calls:
        assert "x" * 64 not in json.dumps(payload or {})
        assert "dop_v1_" not in json.dumps(payload or {})


def test_create_request_bodies_carry_labels_user_data_and_keys():
    provider, api = hetzner_provider()
    image = provider.find_image()
    key = provider.ensure_ssh_key(
        "k",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBf4IlMZd2P1MJzUwQSpDAqDNQJKpPRdchhWExMrkslI t",
        LABELS,
    )
    provider.create_vm(
        VMSpec(
            name="n",
            region="fsn1",
            size="cx22",
            image=image,
            ssh_keys=[key],
            user_data="#cloud-config\nhostname: n\n",
            labels=LABELS,
            enable_ipv6=False,
        )
    )
    body = next(j for m, p, j in api.calls if m == "POST" and p == "/servers")
    assert (
        body["labels"] == LABELS
        and body["ssh_keys"] == [77]
        and body["user_data"].startswith("#cloud-config")
    )
    assert (
        body["public_net"] == {"enable_ipv4": True, "enable_ipv6": False}
        and body["start_after_create"] is True
    )

    provider, api = digitalocean_provider()
    image = provider.find_image()
    key = provider.ensure_ssh_key("k", "ssh-ed25519 AAAA t", LABELS)
    provider.create_vm(
        VMSpec(
            name="n",
            region="fra1",
            size="s-2vcpu-4gb",
            image=image,
            ssh_keys=[key],
            user_data="#cloud-config\n",
            labels=LABELS,
        )
    )
    body = next(j for m, p, j in api.calls if m == "POST" and p == "/droplets")
    assert (
        set(labels_to_tags(LABELS)) <= set(body["tags"])
        and body["project_id"] == "proj-uuid"
        and body["ssh_keys"] == [55]
    )
    assert tags_to_labels(body["tags"])[LABEL_OWNER] == "owner1"


def test_hetzner_deprecated_and_arm_sizes_are_not_auto_selected():
    provider, _ = hetzner_provider()
    sizes = {s.id: s for s in provider.list_sizes("fsn1")}
    assert sizes["cx11"].deprecated and sizes["cax11"].architecture == "arm"
    assert provider.choose_size("fsn1", 1000, 10).id == "cx22"
    assert "cx32" not in {s.id for s in provider.list_sizes("nbg1")}


def test_static_provider_and_registry(monkeypatch):
    monkeypatch.setenv("BBIC_STATIC_HOST", "10.42.0.20:2222")
    provider = create_provider("static", {}, {})
    assert isinstance(provider, StaticHostProvider) and provider.ssh_port == 2222
    vm = provider.create_vm(
        VMSpec(
            name="x",
            region="local",
            size="static",
            image=provider.find_image(),
            ssh_keys=[],
            user_data="",
            labels=LABELS,
        )
    )
    assert vm.address == "10.42.0.20" and provider.wait_for_vm(vm.id).status == "running"
    provider.destroy_vm(vm.id)
    with pytest.raises(ConfigurationError):
        create_provider("hetzner", {}, {})
    with pytest.raises(ConfigurationError):
        create_provider("nope", {}, {})


def test_maintenance_ssh_port_is_provider_defined():
    hetzner, _ = hetzner_provider()
    assert hetzner.maintenance_ssh_port(22022) == 22022
    static = StaticHostProvider("10.0.0.5", 2222)
    assert static.maintenance_ssh_port(22022) == 2222

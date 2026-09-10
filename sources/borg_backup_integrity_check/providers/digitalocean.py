"""DigitalOcean backend (https://docs.digitalocean.com/reference/api/).

DigitalOcean uses flat tags instead of key/value labels, so our labels are
flattened to ``key-value`` tags. Optional project assignment moves the
droplet/volume into a chosen project after creation.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from ..errors import ProviderError
from ..logging_setup import get_logger
from .base import (
    LABEL_MANAGED,
    LABEL_OWNER,
    LABEL_RUN,
    MANAGED_BY,
    CloudProvider,
    MachineSize,
    ManagedResource,
    OSImage,
    ProviderCapabilities,
    Region,
    SSHKeyRef,
    VMInfo,
    VMSpec,
    VolumeInfo,
    labels_to_tags,
    tags_to_labels,
)
from .http import JsonApi

log = get_logger("providers.digitalocean")
API = "https://api.digitalocean.com/v2"


class DigitalOceanProvider(CloudProvider):
    name = "digitalocean"
    display_name = "DigitalOcean"
    capabilities = ProviderCapabilities(volumes=True, pricing=True, tags_are_key_value=False)

    def __init__(self, token: str, project: str | None = None, api: JsonApi | None = None) -> None:
        self.api = api or JsonApi(API, token)
        self.project = project or None
        self._project_id: str | None = None

    # ---------------------------------------------------------------- account
    def validate_credentials(self) -> str:
        account = self.api.get("/account").get("account", {})
        status = account.get("status", "?")
        project = self._resolve_project() if self.project else None
        text = f"DigitalOcean account {account.get('email', '?')} ({status}, droplet limit {account.get('droplet_limit', '?')})"
        if self.project:
            text += f", project {self.project!r} " + ("found" if project else "NOT FOUND")
            if not project:
                raise ProviderError(text)
        return text

    def list_regions(self) -> list[Region]:
        data = self.api.paginate("/regions", "regions", per_page=200)
        return [
            Region(
                id=r["slug"],
                name=r.get("name", r["slug"]),
                available=bool(r.get("available", True)),
                extra={"sizes": r.get("sizes", [])},
            )
            for r in data
        ]

    def list_sizes(self, region: str | None = None) -> list[MachineSize]:
        sizes: list[MachineSize] = []
        for s in self.api.paginate("/sizes", "sizes", per_page=200):
            regions = tuple(s.get("regions", []) or [])
            if region and regions and region not in regions:
                continue
            desc = str(s.get("description", ""))
            sizes.append(
                MachineSize(
                    id=s["slug"],
                    name=s["slug"],
                    vcpus=int(s.get("vcpus", 0)),
                    memory_mb=int(s.get("memory", 0)),
                    disk_gb=int(s.get("disk", 0)),
                    price_hourly=float(s["price_hourly"])
                    if s.get("price_hourly") is not None
                    else None,
                    price_monthly=float(s["price_monthly"])
                    if s.get("price_monthly") is not None
                    else None,
                    architecture="x86",
                    regions=regions,
                    available=bool(s.get("available", True)),
                    extra={"description": desc, "gpu": bool(s.get("gpu_info"))},
                )
            )
        return sizes

    def find_image(
        self, family: str = "debian", version: str = "12", architecture: str = "x86"
    ) -> OSImage:
        images = self.api.paginate(
            "/images", "images", params={"type": "distribution"}, per_page=200
        )
        wanted = f"{family}-{version}-x64"
        for img in images:
            if img.get("slug") == wanted and img.get("status") == "available":
                return OSImage(
                    id=str(img["slug"]),
                    name=img.get("name", wanted),
                    family=family,
                    version=str(version),
                )
        for img in images:
            if (
                str(img.get("distribution", "")).lower() == family
                and str(img.get("name", "")).startswith(f"{version} ")
                and img.get("status") == "available"
            ):
                return OSImage(
                    id=str(img.get("slug") or img["id"]),
                    name=img.get("name", ""),
                    family=family,
                    version=str(version),
                )
        raise ProviderError(f"no available DigitalOcean distribution image for {family} {version}")

    # --------------------------------------------------------------- ssh keys
    def ensure_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> SSHKeyRef:
        for key in self.api.paginate("/account/keys", "ssh_keys", per_page=200):
            if key.get("public_key", "").split()[:2] == public_key.split()[:2]:
                return SSHKeyRef(
                    id=str(key["id"]), name=key.get("name", ""), fingerprint=key.get("fingerprint")
                )
        data = self.api.post("/account/keys", json={"name": name, "public_key": public_key})
        key = data["ssh_key"]
        return SSHKeyRef(
            id=str(key["id"]), name=key.get("name", name), fingerprint=key.get("fingerprint")
        )

    def delete_ssh_key(self, key: SSHKeyRef) -> None:
        self.api.delete(f"/account/keys/{key.id}", ok=(204, 404))

    # ---------------------------------------------------------------- droplets
    def create_vm(self, spec: VMSpec) -> VMInfo:
        body: dict[str, Any] = {
            "name": spec.name,
            "region": spec.region,
            "size": spec.size,
            "image": spec.image.id,
            "ssh_keys": [int(k.id) for k in spec.ssh_keys],
            "user_data": spec.user_data,
            "tags": labels_to_tags(spec.labels),
            "ipv6": spec.enable_ipv6,
            "monitoring": False,
            "backups": False,
            "with_droplet_agent": False,
        }
        project_id = self._resolve_project() if self.project else None
        if project_id:
            body["project_id"] = project_id
        data = self.api.post("/droplets", json=body)
        vm = _droplet_to_vm(data["droplet"])
        log.info("created DigitalOcean droplet %s (%s) in %s", vm.name, vm.id, vm.region)
        return vm

    def get_vm(self, vm_id: str) -> VMInfo | None:
        try:
            data = self.api.get(f"/droplets/{vm_id}")
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise
        return _droplet_to_vm(data["droplet"])

    def wait_for_vm(self, vm_id: str, timeout: int = 600) -> VMInfo:
        deadline = time.time() + timeout
        while time.time() < deadline:
            vm = self.get_vm(vm_id)
            if vm is None:
                raise ProviderError(f"droplet {vm_id} disappeared while waiting for it to start")
            if vm.status == "running" and vm.address:
                return vm
            time.sleep(5)
        raise ProviderError(f"droplet {vm_id} did not become active within {timeout}s")

    def destroy_vm(self, vm_id: str) -> None:
        self.api.delete(f"/droplets/{vm_id}", ok=(204, 404))
        deadline = time.time() + 300
        while time.time() < deadline and self.get_vm(vm_id) is not None:
            time.sleep(3)

    # ---------------------------------------------------------------- volumes
    def create_volume(
        self,
        name: str,
        size_gb: int,
        region: str,
        labels: dict[str, str],
        attach_to: str | None = None,
    ) -> VolumeInfo:
        body = {
            "name": name,
            "size_gigabytes": size_gb,
            "region": region,
            "filesystem_type": "ext4",
            "tags": labels_to_tags(labels),
        }
        data = self.api.post("/volumes", json=body)
        volume = _volume_to_info(data["volume"])
        if attach_to:
            action = self.api.post(
                "/volumes/actions",
                json={
                    "type": "attach",
                    "volume_name": name,
                    "droplet_id": int(attach_to),
                    "region": region,
                },
            )
            self._wait_action(action.get("action", {}))
            volume = self.get_volume(volume.id) or volume
        return volume

    def get_volume(self, volume_id: str) -> VolumeInfo | None:
        try:
            data = self.api.get(f"/volumes/{volume_id}")
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise
        return _volume_to_info(data["volume"])

    def destroy_volume(self, volume_id: str) -> None:
        volume = self.get_volume(volume_id)
        if volume is None:
            return
        if volume.attached_to:
            action = self.api.post(
                f"/volumes/{volume_id}/actions",
                json={"type": "detach", "droplet_id": int(volume.attached_to)},
            )
            self._wait_action(action.get("action", {}))
        self.api.delete(f"/volumes/{volume_id}", ok=(204, 404))

    # -------------------------------------------------------------- discovery
    def list_managed_resources(self, owner: str | None = None) -> list[ManagedResource]:
        tag = f"{LABEL_MANAGED}-{MANAGED_BY}"
        found: list[ManagedResource] = []
        for droplet in self.api.paginate(
            "/droplets", "droplets", params={"tag_name": tag}, per_page=200
        ):
            labels = tags_to_labels(droplet.get("tags", []) or [])
            if owner and labels.get(LABEL_OWNER) != owner:
                continue
            found.append(
                ManagedResource(
                    "server",
                    str(droplet["id"]),
                    droplet.get("name", ""),
                    labels.get(LABEL_RUN),
                    labels.get(LABEL_OWNER),
                    _parse_time(droplet.get("created_at")),
                    {"status": droplet.get("status"), "labels": labels},
                )
            )
        for volume in self.api.paginate("/volumes", "volumes", per_page=200):
            labels = tags_to_labels(volume.get("tags", []) or [])
            if labels.get(LABEL_MANAGED) != MANAGED_BY:
                continue
            if owner and labels.get(LABEL_OWNER) != owner:
                continue
            found.append(
                ManagedResource(
                    "volume",
                    str(volume["id"]),
                    volume.get("name", ""),
                    labels.get(LABEL_RUN),
                    labels.get(LABEL_OWNER),
                    _parse_time(volume.get("created_at")),
                    {"labels": labels, "droplet_ids": volume.get("droplet_ids", [])},
                )
            )
        return found

    # ---------------------------------------------------------------- helpers
    def _resolve_project(self) -> str | None:
        if self._project_id or not self.project:
            return self._project_id
        for project in self.api.paginate("/projects", "projects", per_page=200):
            if project.get("id") == self.project or project.get("name") == self.project:
                self._project_id = project["id"]
                return self._project_id
        return None

    def _wait_action(self, action: dict[str, Any], timeout: int = 300) -> None:
        action_id = action.get("id")
        if not action_id:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = self.api.get(f"/actions/{action_id}")["action"]
            if current["status"] == "completed":
                return
            if current["status"] == "errored":
                raise ProviderError(f"DigitalOcean action {current.get('type')} errored")
            time.sleep(3)
        raise ProviderError(f"DigitalOcean action {action_id} did not finish within {timeout}s")


def _droplet_to_vm(d: dict[str, Any]) -> VMInfo:
    ipv4 = ipv6 = None
    for net in (d.get("networks") or {}).get("v4", []) or []:
        if net.get("type") == "public":
            ipv4 = net.get("ip_address")
    for net in (d.get("networks") or {}).get("v6", []) or []:
        if net.get("type") == "public":
            ipv6 = net.get("ip_address")
    status = {
        "new": "provisioning",
        "active": "running",
        "off": "stopped",
        "archive": "stopped",
    }.get(d.get("status", ""), "unknown")
    return VMInfo(
        id=str(d["id"]),
        name=d.get("name", ""),
        status=status,
        ipv4=ipv4,
        ipv6=ipv6,
        region=(d.get("region") or {}).get("slug"),
        size=d.get("size_slug"),
        labels=tags_to_labels(d.get("tags", []) or []),
        created_at=_parse_time(d.get("created_at")),
        volume_ids=[str(v) for v in d.get("volume_ids", []) or []],
    )


def _volume_to_info(v: dict[str, Any]) -> VolumeInfo:
    droplets = v.get("droplet_ids", []) or []
    return VolumeInfo(
        id=str(v["id"]),
        name=v.get("name", ""),
        size_gb=int(v.get("size_gigabytes", 0)),
        status="available",
        attached_to=str(droplets[0]) if droplets else None,
        device=f"/dev/disk/by-id/scsi-0DO_Volume_{v.get('name')}" if v.get("name") else None,
        labels=tags_to_labels(v.get("tags", []) or []),
        created_at=_parse_time(v.get("created_at")),
    )


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

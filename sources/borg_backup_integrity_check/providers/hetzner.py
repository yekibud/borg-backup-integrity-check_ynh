"""Hetzner Cloud backend (https://docs.hetzner.cloud/).

Tokens are project scoped, so no project name is required. Servers, volumes
and SSH keys are labelled with the run id and owner hash; cleanup only ever
touches resources selected by those labels.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from ..errors import ProviderError
from ..logging_setup import get_logger
from .base import (
    LABEL_CREATED,
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
)
from .http import JsonApi

log = get_logger("providers.hetzner")
API = "https://api.hetzner.cloud/v1"


class HetznerCloudProvider(CloudProvider):
    name = "hetzner"
    display_name = "Hetzner Cloud"
    capabilities = ProviderCapabilities(volumes=True, ipv4_optional=True, pricing=True)

    def __init__(self, token: str, api: JsonApi | None = None) -> None:
        self.api = api or JsonApi(API, token)

    # ---------------------------------------------------------------- account
    def validate_credentials(self) -> str:
        data = self.api.get("/servers", params={"per_page": 1})
        total = (data.get("meta", {}).get("pagination", {}) or {}).get("total_entries", "?")
        locations = self.list_regions()
        return f"Hetzner project reachable ({total} servers, {len(locations)} locations available)"

    def list_regions(self) -> list[Region]:
        data = self.api.paginate("/locations", "locations")
        return [
            Region(
                id=loc["name"],
                name=f"{loc.get('city', '')} ({loc.get('description', '')})".strip(),
                country=loc.get("country"),
            )
            for loc in data
        ]

    def list_sizes(self, region: str | None = None) -> list[MachineSize]:
        sizes: list[MachineSize] = []
        for st in self.api.paginate("/server_types", "server_types"):
            locs = st.get("locations") or []
            supported = tuple(loc.get("name") for loc in locs if loc.get("name"))
            deprecated_here = False
            available = True
            if region:
                match = next((loc for loc in locs if loc.get("name") == region), None)
                if locs and match is None:
                    continue
                if match and match.get("deprecation"):
                    deprecated_here = True
                if match and match.get("available") is False:
                    available = False
            price_hourly = price_monthly = None
            for price in st.get("prices", []) or []:
                if region is None or price.get("location") == region:
                    try:
                        price_hourly = float(price["price_hourly"]["gross"])
                        price_monthly = float(price["price_monthly"]["gross"])
                    except (KeyError, TypeError, ValueError):
                        pass
                    break
            sizes.append(
                MachineSize(
                    id=st["name"],
                    name=st["name"],
                    vcpus=int(st.get("cores", 0)),
                    memory_mb=int(float(st.get("memory", 0)) * 1024),
                    disk_gb=int(st.get("disk", 0)),
                    price_hourly=price_hourly,
                    price_monthly=price_monthly,
                    architecture="arm" if st.get("architecture") == "arm" else "x86",
                    regions=supported,
                    available=available,
                    deprecated=bool(st.get("deprecation")) or deprecated_here,
                    extra={"cpu_type": st.get("cpu_type"), "category": st.get("category")},
                )
            )
        return sizes

    def find_image(
        self, family: str = "debian", version: str = "12", architecture: str = "x86"
    ) -> OSImage:
        images = self.api.paginate(
            "/images",
            "images",
            params={"type": "system", "architecture": architecture, "status": "available"},
        )
        for img in images:
            if (
                img.get("os_flavor") == family
                and str(img.get("os_version", "")).split(".")[0] == str(version)
                and not img.get("deprecated")
            ):
                return OSImage(
                    id=str(img["id"]),
                    name=img.get("name") or str(img["id"]),
                    family=family,
                    version=str(version),
                    architecture=architecture,
                )
        raise ProviderError(
            f"no available Hetzner system image for {family} {version} ({architecture})"
        )

    # --------------------------------------------------------------- ssh keys
    def ensure_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> SSHKeyRef:
        fingerprint = _md5_fingerprint(public_key)
        existing = (
            self.api.get("/ssh_keys", params={"fingerprint": fingerprint}).get("ssh_keys", [])
            if fingerprint
            else []
        )
        if existing:
            key = existing[0]
            return SSHKeyRef(
                id=str(key["id"]), name=key["name"], fingerprint=key.get("fingerprint")
            )
        data = self.api.post(
            "/ssh_keys", json={"name": name, "public_key": public_key, "labels": labels}
        )
        key = data["ssh_key"]
        return SSHKeyRef(id=str(key["id"]), name=key["name"], fingerprint=key.get("fingerprint"))

    def delete_ssh_key(self, key: SSHKeyRef) -> None:
        self.api.delete(f"/ssh_keys/{key.id}", ok=(204, 404))

    # ---------------------------------------------------------------- servers
    def create_vm(self, spec: VMSpec) -> VMInfo:
        body: dict[str, Any] = {
            "name": spec.name,
            "server_type": spec.size,
            "image": spec.image.id,
            "location": spec.region,
            "ssh_keys": [int(k.id) for k in spec.ssh_keys],
            "user_data": spec.user_data,
            "labels": spec.labels,
            "start_after_create": True,
            "public_net": {"enable_ipv4": spec.enable_ipv4, "enable_ipv6": spec.enable_ipv6},
        }
        data = self.api.post("/servers", json=body)
        vm = _server_to_vm(data["server"])
        log.info("created Hetzner server %s (%s) in %s", vm.name, vm.id, vm.region)
        return vm

    def get_vm(self, vm_id: str) -> VMInfo | None:
        try:
            data = self.api.get(f"/servers/{vm_id}")
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise
        return _server_to_vm(data["server"])

    def wait_for_vm(self, vm_id: str, timeout: int = 600) -> VMInfo:
        deadline = time.time() + timeout
        while time.time() < deadline:
            vm = self.get_vm(vm_id)
            if vm is None:
                raise ProviderError(f"server {vm_id} disappeared while waiting for it to start")
            if vm.status == "running" and vm.address:
                return vm
            time.sleep(5)
        raise ProviderError(
            f"server {vm_id} did not reach 'running' within {timeout}s", retryable=False
        )

    def destroy_vm(self, vm_id: str) -> None:
        try:
            data = self.api.delete(f"/servers/{vm_id}", ok=(200, 404))
        except ProviderError as exc:
            if exc.status == 404:
                return
            raise
        action = (data or {}).get("action") if isinstance(data, dict) else None
        if action:
            self._wait_action(action["id"])
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
        body: dict[str, Any] = {"name": name, "size": size_gb, "labels": labels, "format": "ext4"}
        if attach_to:
            body["server"] = int(attach_to)
            body["automount"] = False
        else:
            body["location"] = region
        data = self.api.post("/volumes", json=body)
        for action in [data.get("action")] + list(data.get("next_actions", []) or []):
            if action:
                self._wait_action(action["id"])
        return _volume_to_info(data["volume"])

    def get_volume(self, volume_id: str) -> VolumeInfo | None:
        try:
            data = self.api.get(f"/volumes/{volume_id}")
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise
        return _volume_to_info(data["volume"])

    def destroy_volume(self, volume_id: str) -> None:
        vol = self.get_volume(volume_id)
        if vol is None:
            return
        if vol.attached_to:
            data = self.api.post(f"/volumes/{volume_id}/actions/detach", json={})
            self._wait_action(data["action"]["id"])
        self.api.delete(f"/volumes/{volume_id}", ok=(204, 404))

    # -------------------------------------------------------------- discovery
    def list_managed_resources(self, owner: str | None = None) -> list[ManagedResource]:
        selector = f"{LABEL_MANAGED}=={MANAGED_BY}"
        if owner:
            selector += f",{LABEL_OWNER}=={owner}"
        found: list[ManagedResource] = []
        for server in self.api.paginate("/servers", "servers", params={"label_selector": selector}):
            found.append(_managed("server", server))
        for volume in self.api.paginate("/volumes", "volumes", params={"label_selector": selector}):
            found.append(_managed("volume", volume))
        for key in self.api.paginate("/ssh_keys", "ssh_keys", params={"label_selector": selector}):
            found.append(_managed("ssh_key", key))
        return found

    # ---------------------------------------------------------------- helpers
    def _wait_action(self, action_id: int | str, timeout: int = 300) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            action = self.api.get(f"/actions/{action_id}")["action"]
            if action["status"] == "success":
                return
            if action["status"] == "error":
                err = action.get("error") or {}
                raise ProviderError(
                    f"Hetzner action {action.get('command')} failed: {err.get('code')}: {err.get('message')}"
                )
            time.sleep(3)
        raise ProviderError(f"Hetzner action {action_id} did not finish within {timeout}s")


def _server_to_vm(server: dict[str, Any]) -> VMInfo:
    public = server.get("public_net") or {}
    ipv6 = (public.get("ipv6") or {}).get("ip")
    if ipv6 and "/" in ipv6:
        # Hetzner assigns a /64; the ::1 address is configured on the server.
        ipv6 = (
            ipv6.split("/")[0].rstrip(":") + "::1"
            if ipv6.split("/")[0].endswith("::")
            else ipv6.split("/")[0]
        )
    status = server.get("status", "unknown")
    mapped = {
        "running": "running",
        "initializing": "provisioning",
        "starting": "provisioning",
        "off": "stopped",
        "stopping": "stopped",
        "deleting": "deleting",
    }.get(status, "unknown")
    return VMInfo(
        id=str(server["id"]),
        name=server.get("name", ""),
        status=mapped,
        ipv4=(public.get("ipv4") or {}).get("ip"),
        ipv6=ipv6,
        region=(server.get("datacenter") or {}).get("location", {}).get("name")
        or (server.get("location") or {}).get("name"),
        size=(server.get("server_type") or {}).get("name"),
        labels=dict(server.get("labels") or {}),
        created_at=_parse_time(server.get("created")),
        volume_ids=[str(v) for v in server.get("volumes", []) or []],
    )


def _volume_to_info(vol: dict[str, Any]) -> VolumeInfo:
    return VolumeInfo(
        id=str(vol["id"]),
        name=vol.get("name", ""),
        size_gb=int(vol.get("size", 0)),
        status=vol.get("status", "unknown"),
        attached_to=str(vol["server"]) if vol.get("server") else None,
        device=vol.get("linux_device"),
        labels=dict(vol.get("labels") or {}),
        created_at=_parse_time(vol.get("created")),
    )


def _managed(kind: str, obj: dict[str, Any]) -> ManagedResource:
    labels = obj.get("labels") or {}
    return ManagedResource(
        kind=kind,
        id=str(obj["id"]),
        name=obj.get("name", ""),
        run_id=labels.get(LABEL_RUN),
        owner=labels.get(LABEL_OWNER),
        created_at=_parse_time(obj.get("created")) or _parse_label_time(labels.get(LABEL_CREATED)),
        extra={"status": obj.get("status"), "labels": labels},
    )


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_label_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%S")
    except ValueError:
        return None


def _md5_fingerprint(public_key: str) -> str | None:
    import base64
    import hashlib

    parts = public_key.strip().split()
    if len(parts) < 2:
        return None
    try:
        raw = base64.b64decode(parts[1])
    except ValueError:
        return None
    digest = hashlib.md5(raw).hexdigest()  # noqa: S324 - SSH fingerprint format, not security
    return ":".join(digest[i : i + 2] for i in range(0, 32, 2))

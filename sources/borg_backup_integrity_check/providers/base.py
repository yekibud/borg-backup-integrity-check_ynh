"""CloudProvider: the provider-neutral contract for disposable restore hosts.

Restore, sampling, verification, reporting and scheduling code only ever sees
the types defined here. Provider-specific response objects never leak out.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MANAGED_BY = "borg-backup-integrity-check"
LABEL_MANAGED = "bbic-managed"
LABEL_RUN = "bbic-run"
LABEL_OWNER = "bbic-owner"  # hash of the production host so several servers may share an account
LABEL_CREATED = "bbic-created"
LABEL_ROLE = "bbic-role"


@dataclass(frozen=True)
class ProviderCapabilities:
    regions: bool = True
    sizes: bool = True
    volumes: bool = False
    ipv6: bool = True
    ipv4_optional: bool = False
    user_data: bool = True
    pricing: bool = False
    tags_are_key_value: bool = True
    ssh_keys_registry: bool = True
    disposable: bool = (
        True  # False when "destroy" cannot actually wipe the host (pre-existing host)
    )


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    available: bool = True
    country: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MachineSize:
    id: str
    name: str
    vcpus: int
    memory_mb: int
    disk_gb: int
    price_hourly: float | None = None
    price_monthly: float | None = None
    architecture: str = "x86"
    regions: tuple[str, ...] = ()
    available: bool = True
    deprecated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OSImage:
    id: str
    name: str
    family: str  # debian
    version: str  # "12" / "13"
    architecture: str = "x86"


@dataclass(frozen=True)
class SSHKeyRef:
    id: str
    name: str
    fingerprint: str | None = None


@dataclass
class VMSpec:
    name: str
    region: str
    size: str
    image: OSImage
    ssh_keys: list[SSHKeyRef]
    user_data: str
    labels: dict[str, str]
    enable_ipv4: bool = True
    enable_ipv6: bool = True
    extra_disk_gb: int = 0  # request a volume of this size when > 0 (if supported)


@dataclass
class VMInfo:
    id: str
    name: str
    status: str  # provisioning | running | stopped | deleting | unknown
    ipv4: str | None = None
    ipv6: str | None = None
    region: str | None = None
    size: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    created_at: datetime | None = None
    volume_ids: list[str] = field(default_factory=list)

    @property
    def address(self) -> str | None:
        return self.ipv4 or self.ipv6

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "ipv4": self.ipv4,
            "ipv6": self.ipv6,
            "region": self.region,
            "size": self.size,
            "labels": dict(self.labels),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "volume_ids": list(self.volume_ids),
        }


@dataclass
class VolumeInfo:
    id: str
    name: str
    size_gb: int
    status: str
    attached_to: str | None = None
    device: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass
class ManagedResource:
    """Any resource (server, volume, ssh key) this application created."""

    kind: str  # server | volume | ssh_key
    id: str
    name: str
    run_id: str | None
    owner: str | None
    created_at: datetime | None
    extra: dict[str, Any] = field(default_factory=dict)


class CloudProvider(ABC):
    """Lifecycle contract every backend implements (see ``providers/hetzner.py``)."""

    name: str = "abstract"
    display_name: str = "Abstract"
    capabilities: ProviderCapabilities = ProviderCapabilities()

    # --- credentials / catalogue ------------------------------------
    @abstractmethod
    def validate_credentials(self) -> str:
        """Return a short human description of the authenticated account/project or raise."""

    @abstractmethod
    def list_regions(self) -> list[Region]: ...

    @abstractmethod
    def list_sizes(self, region: str | None = None) -> list[MachineSize]: ...

    @abstractmethod
    def find_image(
        self, family: str = "debian", version: str = "12", architecture: str = "x86"
    ) -> OSImage: ...

    # --- ssh keys ------------------------------------------------------
    @abstractmethod
    def ensure_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> SSHKeyRef: ...

    @abstractmethod
    def delete_ssh_key(self, key: SSHKeyRef) -> None: ...

    # --- servers -------------------------------------------------------
    @abstractmethod
    def create_vm(self, spec: VMSpec) -> VMInfo: ...

    @abstractmethod
    def get_vm(self, vm_id: str) -> VMInfo | None: ...

    @abstractmethod
    def wait_for_vm(self, vm_id: str, timeout: int = 600) -> VMInfo: ...

    @abstractmethod
    def destroy_vm(self, vm_id: str) -> None: ...

    # --- volumes (optional capability) -----------------------------------
    def create_volume(
        self,
        name: str,
        size_gb: int,
        region: str,
        labels: dict[str, str],
        attach_to: str | None = None,
    ) -> VolumeInfo:
        raise NotImplementedError(f"{self.display_name} backend does not support volumes")

    def destroy_volume(self, volume_id: str) -> None:
        raise NotImplementedError(f"{self.display_name} backend does not support volumes")

    def get_volume(self, volume_id: str) -> VolumeInfo | None:
        return None

    # --- discovery of what we own ---------------------------------------
    @abstractmethod
    def list_managed_resources(self, owner: str | None = None) -> list[ManagedResource]:
        """Every server/volume/key carrying our management labels (optionally for one owner)."""

    # --- host access -------------------------------------------------------
    def maintenance_ssh_port(self, configured: int) -> int:
        """Port of the maintenance sshd on the host (cloud hosts: what cloud-init configured)."""
        return configured

    # --- helpers for sizing ------------------------------------------------
    def choose_size(
        self,
        region: str,
        min_memory_mb: int,
        min_disk_gb: int,
        preferred: str | None = None,
        architecture: str = "x86",
    ) -> MachineSize:
        sizes = [
            s
            for s in self.list_sizes(region)
            if s.available and not s.deprecated and s.architecture == architecture
        ]
        if preferred and preferred != "auto":
            for size in sizes:
                if size.id == preferred or size.name == preferred:
                    return size
            raise LookupError(f"machine size {preferred!r} is not available in region {region!r}")
        fitting = [s for s in sizes if s.memory_mb >= min_memory_mb and s.disk_gb >= min_disk_gb]
        if not fitting:
            raise LookupError(
                f"no machine size in {region!r} offers >= {min_memory_mb} MB RAM and >= {min_disk_gb} GB disk"
            )
        fitting.sort(
            key=lambda s: (
                s.price_hourly if s.price_hourly is not None else 1e9,
                s.memory_mb,
                s.disk_gb,
            )
        )
        return fitting[0]


def run_labels(run_id: str, owner: str, role: str = "restore-host") -> dict[str, str]:
    return {
        LABEL_MANAGED: MANAGED_BY,
        LABEL_RUN: run_id,
        LABEL_OWNER: owner,
        LABEL_CREATED: datetime.now().strftime("%Y%m%dT%H%M%S"),
        LABEL_ROLE: role,
    }


def labels_to_tags(labels: dict[str, str]) -> list[str]:
    """Flatten key/value labels into ``key-value`` tag strings for tag-only providers."""
    return [f"{k}-{v}" if v else k for k, v in labels.items()]


def tags_to_labels(tags: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for tag in tags:
        for key in (LABEL_MANAGED, LABEL_RUN, LABEL_OWNER, LABEL_CREATED, LABEL_ROLE):
            if tag == key:
                labels[key] = ""
            elif tag.startswith(key + "-"):
                labels[key] = tag[len(key) + 1 :]
    return labels

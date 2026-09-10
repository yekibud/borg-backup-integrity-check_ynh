"""StaticHostProvider: an already existing host (development / local VirtualBox end-to-end tests).

It implements the same contract but cannot provision or destroy anything:
"create" returns the configured host, "destroy" is a no-op that logs a reminder.
Never offered in the YunoHost install form; selected with ``--provider static``
or the ``BBIC_STATIC_HOST`` environment variable for testing.
"""

from __future__ import annotations

import os
from datetime import datetime

from ..errors import ConfigurationError
from ..logging_setup import get_logger
from .base import (
    CloudProvider,
    MachineSize,
    ManagedResource,
    OSImage,
    ProviderCapabilities,
    Region,
    SSHKeyRef,
    VMInfo,
    VMSpec,
)

log = get_logger("providers.static")


class StaticHostProvider(CloudProvider):
    name = "static"
    display_name = "Static host (testing)"
    capabilities = ProviderCapabilities(
        regions=False,
        sizes=False,
        volumes=False,
        pricing=False,
        user_data=False,
        ssh_keys_registry=False,
    )

    def __init__(self, address: str, ssh_port: int = 22) -> None:
        self.address = address
        self.ssh_port = ssh_port

    @classmethod
    def from_settings(cls, settings: dict[str, object]) -> StaticHostProvider:
        address = os.environ.get("BBIC_STATIC_HOST") or str(settings.get("static_host") or "")
        if not address:
            raise ConfigurationError(
                "static provider needs BBIC_STATIC_HOST=<address> (optionally address:port)"
            )
        port = int(os.environ.get("BBIC_STATIC_SSH_PORT") or settings.get("static_ssh_port") or 22)
        if ":" in address and not address.startswith("["):
            host, _, maybe_port = address.rpartition(":")
            if maybe_port.isdigit():
                address, port = host, int(maybe_port)
        return cls(address, port)

    def validate_credentials(self) -> str:
        return f"static host {self.address}:{self.ssh_port} (no credentials needed)"

    def list_regions(self) -> list[Region]:
        return [Region(id="local", name="Local")]

    def list_sizes(self, region: str | None = None) -> list[MachineSize]:
        return [MachineSize(id="static", name="static", vcpus=0, memory_mb=0, disk_gb=0)]

    def choose_size(
        self,
        region: str,
        min_memory_mb: int,
        min_disk_gb: int,
        preferred: str | None = None,
        architecture: str = "x86",
    ) -> MachineSize:
        return self.list_sizes()[0]

    def find_image(
        self, family: str = "debian", version: str = "12", architecture: str = "x86"
    ) -> OSImage:
        return OSImage(id="existing", name="existing installation", family=family, version=version)

    def ensure_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> SSHKeyRef:
        return SSHKeyRef(id="static", name=name)

    def delete_ssh_key(self, key: SSHKeyRef) -> None:
        return None

    def create_vm(self, spec: VMSpec) -> VMInfo:
        log.info(
            "static provider: using existing host %s:%s as %s",
            self.address,
            self.ssh_port,
            spec.name,
        )
        return VMInfo(
            id=f"static:{self.address}:{self.ssh_port}",
            name=spec.name,
            status="running",
            ipv4=self.address,
            labels=spec.labels,
            created_at=datetime.now(),
        )

    def get_vm(self, vm_id: str) -> VMInfo | None:
        return VMInfo(id=vm_id, name="static", status="running", ipv4=self.address)

    def wait_for_vm(self, vm_id: str, timeout: int = 600) -> VMInfo:
        return self.get_vm(vm_id)  # type: ignore[return-value]

    def destroy_vm(self, vm_id: str) -> None:
        log.warning(
            "static provider: host %s is not destroyed; reset it yourself (e.g. restore a VM snapshot)",
            self.address,
        )

    def list_managed_resources(self, owner: str | None = None) -> list[ManagedResource]:
        return []

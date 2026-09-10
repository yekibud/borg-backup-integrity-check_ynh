"""Provider lookup by configured name."""

from __future__ import annotations

from ..errors import ConfigurationError
from .base import CloudProvider

PROVIDER_CHOICES = ("hetzner", "digitalocean")


def create_provider(
    name: str, credentials: dict[str, str], settings: dict[str, object]
) -> CloudProvider:
    if name == "hetzner":
        from .hetzner import HetznerCloudProvider

        token = credentials.get("hetzner_token")
        if not token:
            raise ConfigurationError("Hetzner API token is not configured")
        return HetznerCloudProvider(token)
    if name == "digitalocean":
        from .digitalocean import DigitalOceanProvider

        token = credentials.get("digitalocean_token")
        if not token:
            raise ConfigurationError("DigitalOcean API token is not configured")
        return DigitalOceanProvider(
            token, project=str(settings.get("digitalocean_project") or "") or None
        )
    if name == "static":
        from .static import StaticHostProvider

        return StaticHostProvider.from_settings(settings)
    raise ConfigurationError(f"unknown cloud provider {name!r}")

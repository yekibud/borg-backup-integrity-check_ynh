"""Provider catalogue (regions/sizes) with a small on-disk cache, plus YunoHost config-panel YAML output."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ..errors import IntegrityCheckError
from ..redaction import redact
from .registry import create_provider

CACHE_TTL = 6 * 3600


def catalog_data(
    config: Any, provider_name: str, what: str, region: str, cache_dir: Path | None = None
) -> dict[str, Any]:
    cache_file = (cache_dir / f"catalog-{provider_name}-{region}.json") if cache_dir else None
    if cache_file and cache_file.is_file() and time.time() - cache_file.stat().st_mtime < CACHE_TTL:
        try:
            with open(cache_file, encoding="utf-8") as fh:
                cached = json.load(fh)
            if all(k in cached for k in (["regions", "sizes"] if what == "all" else [what])):
                return cached
        except (OSError, ValueError):
            pass
    try:
        provider = create_provider(provider_name, config.credentials(), config.provider_settings)
        data: dict[str, Any] = {}
        data["regions"] = [
            {"id": r.id, "name": r.name} for r in provider.list_regions() if r.available
        ]
        data["sizes"] = [
            {
                "id": s.id,
                "name": f"{s.id} ({s.vcpus} vCPU, {s.memory_mb // 1024} GB RAM, {s.disk_gb} GB)"
                + (f" ~{s.price_monthly:.2f}/month" if s.price_monthly else ""),
            }
            for s in provider.list_sizes(region)
            if s.available and not s.deprecated and s.architecture == "x86"
        ]
    except IntegrityCheckError as exc:
        return {"error": redact(str(exc))}
    if cache_file:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = cache_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.chmod(tmp, 0o600)
            tmp.replace(cache_file)
        except OSError:
            pass
    return data


def ynh_choices_yaml(items: list[dict[str, str]], current: str, extra: str | None = None) -> str:
    """YAML consumed by a config-panel getter: ``choices:`` mapping plus ``value:``."""
    choices: dict[str, str] = {}
    if extra:
        choices[extra] = "Automatic (cheapest suitable)"
    for item in items:
        choices[item["id"]] = item["name"]
    if current and current not in choices:
        choices[current] = current + (
            " (not found in the provider catalogue)" if items else " (catalogue unavailable)"
        )
    lines = (
        ["choices:"]
        + [f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in choices.items()]
        + [f"value: {json.dumps(current)}"]
    )
    return "\n".join(lines)

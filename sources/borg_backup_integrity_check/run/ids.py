"""Run identifiers and resource names (hostname-safe, unique, sortable)."""

from __future__ import annotations

import secrets
import string
from datetime import datetime

_ALPHABET = string.ascii_lowercase + string.digits


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now()
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(4))
    return f"{now:%Y%m%d-%H%M%S}-{suffix}"


def host_name_for(run_id: str) -> str:
    return f"bbic-{run_id}"


def is_run_id(value: str) -> bool:
    parts = value.split("-")
    return (
        len(parts) == 3
        and parts[0].isdigit()
        and len(parts[0]) == 8
        and parts[1].isdigit()
        and len(parts[2]) == 4
    )

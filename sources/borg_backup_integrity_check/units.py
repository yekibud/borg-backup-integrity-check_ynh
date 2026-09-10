"""Byte/percentage formatting helpers.

Sizes are formatted with decimal SI units (kB, MB, GB, TB) exactly like Borg's
own ``--stats`` output, so numbers in reports match what admins see in Borg.
"""

from __future__ import annotations

_SI = ["B", "kB", "MB", "GB", "TB", "PB"]


def format_bytes(value: int | float | None, precision: int = 1) -> str:
    if value is None:
        return "n/a"
    size = float(value)
    unit = 0
    while abs(size) >= 1000 and unit < len(_SI) - 1:
        size /= 1000.0
        unit += 1
    if unit == 0:
        return f"{int(size)} B"
    return f"{size:.{precision}f} {_SI[unit]}"


def format_count(value: int | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,}"


def pct_change(current: float | None, previous: float | None) -> float | None:
    """Percentage change from ``previous`` to ``current``; ``None`` when not comparable."""
    if current is None or previous is None:
        return None
    if previous == 0:
        if current == 0:
            return 0.0
        return None
    return (current - previous) / previous * 100.0


def format_pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "n/a"
    if signed:
        return f"{value:+.1f}%"
    return f"{value:.1f}%"


def parse_size(text: str) -> int:
    """Parse ``"50M"``/``"2G"``/``"1024"`` style sizes into bytes (binary multiples)."""
    text = text.strip()
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    if text and text[-1].upper() in multipliers:
        return int(float(text[:-1]) * multipliers[text[-1].upper()])
    return int(text)

"""Secret redaction for logs, reports and error messages.

Every subprocess output or exception message that may contain credentials
must go through :func:`redact` (or a :class:`Redactor`) before being logged,
printed or emailed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

_ENV_SECRET_RE = re.compile(
    r"(?i)\b((?:BORG_(?:PASSPHRASE|NEW_PASSPHRASE|PASSCOMMAND)|[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE)[A-Z0-9_]*)\s*[=:]\s*)(['\"]?)([^'\"\s]+)\2"
)
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{8,}")
_URL_CRED_RE = re.compile(r"(://[^/:@\s]+:)[^@/\s]+(@)")
_DO_TOKEN_RE = re.compile(r"\bdop_v1_[0-9a-f]{64}\b")
_HCLOUD_TOKEN_RE = re.compile(r"\b[A-Za-z0-9]{64}\b")

REDACTED = "[REDACTED]"


class Redactor:
    """Replaces known secret values and secret-looking patterns with a placeholder."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: list[str] = []
        self.add_secrets(secrets)

    def add_secrets(self, secrets: Iterable[str]) -> None:
        for secret in secrets:
            if secret and len(secret) >= 4 and secret not in self._secrets:
                self._secrets.append(secret)
        # Longest first so partial overlaps never leak a suffix.
        self._secrets.sort(key=len, reverse=True)

    def redact(self, text: str | None) -> str:
        if not text:
            return "" if text is None else text
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = _ENV_SECRET_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
        text = _BEARER_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
        text = _URL_CRED_RE.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(2)}", text)
        text = _DO_TOKEN_RE.sub(REDACTED, text)
        return text


_global = Redactor()


def register_secret(value: str | None) -> None:
    if value:
        _global.add_secrets([value])


def redact(text: str | None) -> str:
    return _global.redact(text)


def secret_fingerprint(value: str) -> str:
    """Short non-reversible identifier so an admin can tell two credentials apart."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]

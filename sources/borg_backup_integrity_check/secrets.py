"""SecretStore: root-only JSON file holding credentials that must not live in app settings.

YunoHost deliberately does not persist password-type install answers as
settings; this store is the explicit, restricted (0600 root:root) home for
provider tokens and the Borg passphrase. Values are never returned to the
config panel; only a "configured"/"not configured" status and a short
fingerprint are exposed.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .redaction import register_secret, secret_fingerprint

KNOWN_SECRETS = ("hetzner_token", "digitalocean_token", "borg_passphrase")


class SecretStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh) or {}
        for value in data.values():
            if isinstance(value, str):
                register_secret(value)
        return {k: v for k, v in data.items() if isinstance(v, str)}

    def _write(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".secrets.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def get(self, name: str) -> str | None:
        value = self._read().get(name)
        if value:
            register_secret(value)
        return value or None

    def set(self, name: str, value: str) -> None:
        if not value:
            raise ValueError("refusing to store an empty secret")
        data = self._read()
        data[name] = value
        register_secret(value)
        self._write(data)

    def delete(self, name: str) -> bool:
        data = self._read()
        if name not in data:
            return False
        del data[name]
        self._write(data)
        return True

    def is_configured(self, name: str) -> bool:
        return bool(self._read().get(name))

    def status(self, name: str) -> dict[str, str | bool]:
        value = self._read().get(name)
        if not value:
            return {"configured": False, "fingerprint": ""}
        return {"configured": True, "fingerprint": secret_fingerprint(value)}

    def all_values(self) -> dict[str, str]:
        return self._read()

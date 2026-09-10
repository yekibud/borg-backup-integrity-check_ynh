"""HostAgent: deploys this package to the restore host and invokes the on-host helper."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from ..errors import RestoreHostError
from ..logging_setup import get_logger
from ..redaction import redact
from .ssh import SSHSession

log = get_logger("agent")
REMOTE_PKG_DIR = "/root/bbic/pkg"
PACKAGE_DIR = Path(__file__).resolve().parent.parent


class HostAgent:
    def __init__(self, ssh: SSHSession) -> None:
        self.ssh = ssh
        self.deployed = False

    def deploy(self) -> None:
        self.ssh.run("mkdir -p /root/bbic && chmod 700 /root/bbic", check=True, timeout=60)
        self.ssh.put_tree(PACKAGE_DIR, f"{REMOTE_PKG_DIR}/{PACKAGE_DIR.name}")
        self.deployed = True

    def call(
        self,
        command: str,
        spec: dict[str, Any] | None = None,
        args: list[str] | None = None,
        timeout: int = 3600,
    ) -> dict[str, Any]:
        if not self.deployed:
            self.deploy()
        argv = " ".join(shlex.quote(a) for a in (args or []))
        remote = f"PYTHONPATH={REMOTE_PKG_DIR} python3 -m borg_backup_integrity_check.restore.host_helper {command} {argv}"
        stdin = json.dumps(spec).encode("utf-8") if spec is not None else None
        result = self.ssh.run(remote, stdin_data=stdin, timeout=timeout)
        text = result.stdout.strip()
        # The helper prints exactly one JSON document as the last line.
        last_line = text.splitlines()[-1] if text else ""
        try:
            data = json.loads(last_line)
        except ValueError as exc:
            raise RestoreHostError(
                f"helper {command} returned no JSON (rc={result.rc}): {redact(text[-500:])} {result.stderr[-500:]}"
            ) from exc
        if result.rc != 0 and data.get("ok") is not False:
            data["ok"] = False
            data.setdefault("error", result.stderr[-800:])
        log.debug("helper %s -> ok=%s", command, data.get("ok", True))
        return data

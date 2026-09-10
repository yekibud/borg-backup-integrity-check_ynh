"""SSHSession: run commands and copy files on the restore host with the system ``ssh`` binary."""

from __future__ import annotations

import io
import shlex
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import RestoreHostError
from ..logging_setup import get_logger
from ..redaction import redact

log = get_logger("ssh")


@dataclass
class CommandResult:
    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


class SSHSession:
    def __init__(
        self,
        host: str,
        port: int,
        key_path: Path,
        known_hosts: Path,
        user: str = "root",
        connect_timeout: int = 20,
    ) -> None:
        self.host = host
        self.port = port
        self.key_path = Path(key_path)
        self.known_hosts = Path(known_hosts)
        self.user = user
        self.connect_timeout = connect_timeout

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def base_args(self) -> list[str]:
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return [
            "ssh",
            "-p",
            str(self.port),
            "-i",
            str(self.key_path),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=6",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-o",
            "LogLevel=ERROR",
        ]

    def describe(self) -> str:
        return f"ssh -p {self.port} -i {self.key_path} {self.target}"

    # ------------------------------------------------------------------ run
    def run(
        self,
        command: str,
        *,
        timeout: int = 900,
        stdin_data: bytes | None = None,
        check: bool = False,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        remote = command
        if env:
            exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
            remote = f"export {exports}; {command}"
        args = self.base_args() + [self.target, remote]
        log.debug("ssh %s: %s", self.host, redact(command)[:300])
        try:
            proc = subprocess.run(
                args, input=stdin_data, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise RestoreHostError(
                f"command timed out after {timeout}s on {self.host}: {redact(command)[:120]}"
            ) from exc
        result = CommandResult(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            redact(proc.stderr.decode("utf-8", "replace")),
        )
        if result.rc == 255:
            raise RestoreHostError(
                f"ssh connection to {self.host}:{self.port} failed: {result.stderr.strip()[-300:]}"
            )
        if check and not result.ok:
            raise RestoreHostError(
                f"command failed (rc={result.rc}) on {self.host}: {redact(command)[:120]}\n{result.stderr.strip()[-1500:]}"
            )
        return result

    def run_script(
        self, script: str, *, timeout: int = 900, check: bool = True, args: list[str] | None = None
    ) -> CommandResult:
        """Run a bash script passed on stdin (keeps secrets out of argv and remote logs)."""
        argv = " ".join(shlex.quote(a) for a in (args or []))
        return self.run(
            f"bash -s -- {argv}", timeout=timeout, stdin_data=script.encode("utf-8"), check=check
        )

    def wait_ready(self, timeout: int = 600, interval: int = 10) -> None:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                result = self.run("echo bbic-ready", timeout=40)
                if "bbic-ready" in result.stdout:
                    return
                last = result.stderr
            except RestoreHostError as exc:
                last = str(exc)
            time.sleep(interval)
        raise RestoreHostError(
            f"{self.host}:{self.port} did not accept SSH within {timeout}s ({last.strip()[-200:]})"
        )

    # ------------------------------------------------------------- transfer
    def put_bytes(self, data: bytes, remote_path: str, mode: str = "0600") -> None:
        cmd = f"mkdir -p {shlex.quote(str(Path(remote_path).parent))} && cat > {shlex.quote(remote_path)} && chmod {mode} {shlex.quote(remote_path)}"
        self.run(cmd, stdin_data=data, check=True, timeout=300)

    def put_file(self, local: Path, remote_path: str, mode: str = "0600") -> None:
        self.put_bytes(Path(local).read_bytes(), remote_path, mode)

    def put_tree(
        self,
        local_dir: Path,
        remote_dir: str,
        exclude_dirs: tuple[str, ...] = ("__pycache__", ".pytest_cache"),
    ) -> None:
        """Copy a directory tree with tar over ssh (ownership root, files 0644/dirs 0755)."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for path in sorted(Path(local_dir).rglob("*")):
                if any(part in exclude_dirs for part in path.relative_to(local_dir).parts):
                    continue
                info = tar.gettarinfo(str(path), arcname=str(path.relative_to(local_dir)))
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mode = 0o755 if path.is_dir() else 0o644
                if path.is_file():
                    with open(path, "rb") as fh:
                        tar.addfile(info, fh)
                else:
                    tar.addfile(info)
        cmd = f"mkdir -p {shlex.quote(remote_dir)} && tar -xzf - -C {shlex.quote(remote_dir)}"
        self.run(cmd, stdin_data=buffer.getvalue(), check=True, timeout=600)

    def get_bytes(self, remote_path: str, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        result = self.run(f"head -c {max_bytes} {shlex.quote(remote_path)}", timeout=600)
        if not result.ok:
            raise RestoreHostError(
                f"cannot read {remote_path} on {self.host}: {result.stderr.strip()[-200:]}"
            )
        # stdout was decoded; fetch raw again for binary safety.
        args = self.base_args() + [self.target, f"head -c {max_bytes} {shlex.quote(remote_path)}"]
        proc = subprocess.run(args, capture_output=True, timeout=600, check=False)
        return proc.stdout

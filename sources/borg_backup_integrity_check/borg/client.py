"""Thin, safe wrapper around the ``borg`` command line.

Design rules:
* the passphrase only ever travels through the process environment (never argv);
* stderr is captured and redacted before it reaches any log or exception;
* ``BORG_EXIT_CODES=modern`` is used so lock/connection problems can be retried.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import BorgError
from ..logging_setup import get_logger
from ..redaction import redact, register_secret
from .models import ArchiveItem, ArchiveRef, ArchiveStats, RepositoryInfo

log = get_logger("borg")

# Modern exit code families (see Borg 1.4 "borg help exit codes" / frontends docs).
_EXIT_CATEGORIES: list[tuple[range, str]] = [
    (range(10, 22), "repository"),
    (range(40, 49), "key"),
    (range(50, 54), "passphrase"),
    (range(60, 65), "cache"),
    (range(70, 76), "lock"),
    (range(80, 88), "connection"),
    (range(90, 100), "integrity"),
    (range(100, 128), "warning"),
]


def exit_code_category(rc: int) -> str:
    if rc == 0:
        return "ok"
    if rc == 1:
        return "warning"
    if rc == 2:
        return "error"
    for rng, name in _EXIT_CATEGORIES:
        if rc in rng:
            return name
    return "error"


@dataclass
class BorgResult:
    rc: int
    stdout: str
    stderr: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class BorgClient:
    """Runs Borg commands against one repository."""

    binary: str
    repository: str
    passphrase: str | None = None
    ssh_key_path: str | None = None
    ssh_known_hosts: str | None = None
    ssh_port: int | None = None
    remote_path: str | None = None
    lock_wait: int = 900
    base_dir: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    retries: int = 3
    retry_delay: float = 30.0

    def __post_init__(self) -> None:
        if self.passphrase:
            register_secret(self.passphrase)

    # ----------------------------------------------------------------- env
    def environment(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("BORG_")}
        env["BORG_REPO"] = self.repository
        env["BORG_EXIT_CODES"] = "modern"
        env["BORG_RELOCATED_REPO_ACCESS_IS_OK"] = "yes"
        env["BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK"] = "no"
        env["LANG"] = env.get("LANG") or "C.UTF-8"
        if self.passphrase is not None:
            env["BORG_PASSPHRASE"] = self.passphrase
        if self.remote_path:
            env["BORG_REMOTE_PATH"] = self.remote_path
        if self.base_dir:
            env["BORG_BASE_DIR"] = self.base_dir
        env["BORG_RSH"] = self.rsh_command()
        env.update(self.extra_env)
        return env

    def rsh_command(self) -> str:
        parts = ["ssh", "-oBatchMode=yes"]
        if self.ssh_key_path:
            parts += ["-i", self.ssh_key_path, "-oIdentitiesOnly=yes"]
        if self.ssh_known_hosts:
            parts += [
                f"-oUserKnownHostsFile={self.ssh_known_hosts}",
                "-oStrictHostKeyChecking=accept-new",
            ]
        else:
            parts += ["-oStrictHostKeyChecking=accept-new"]
        return " ".join(parts)

    # ------------------------------------------------------------- running
    def _base_args(self) -> list[str]:
        return [self.binary, "--lock-wait", str(self.lock_wait)]

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin_data: bytes | None = None,
        timeout: int | None = None,
        allow_warnings: bool = True,
    ) -> BorgResult:
        """Run a Borg subcommand and return its output; retries lock/connection failures."""
        cmd = self._base_args() + list(args)
        attempt = 0
        while True:
            attempt += 1
            log.debug("borg %s (attempt %d)", " ".join(args), attempt)
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(cwd) if cwd else None,
                    env=self.environment(),
                    input=stdin_data,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise BorgError(f"borg binary not found: {self.binary}", category="config") from exc
            except subprocess.TimeoutExpired as exc:
                raise BorgError(
                    f"borg {args[0]} timed out after {timeout}s", category="timeout"
                ) from exc
            stdout = proc.stdout.decode("utf-8", "replace")
            stderr = redact(proc.stderr.decode("utf-8", "replace"))
            category = exit_code_category(proc.returncode)
            if category == "ok":
                return BorgResult(proc.returncode, stdout, stderr)
            if category == "warning":
                warnings = [line for line in stderr.splitlines() if line.strip()]
                log.warning(
                    "borg %s finished with warnings: %s", args[0], " | ".join(warnings[-3:])
                )
                if allow_warnings:
                    return BorgResult(proc.returncode, stdout, stderr, warnings)
            err = BorgError(
                f"borg {args[0]} failed (rc={proc.returncode}, {category}): {stderr.strip()[-800:]}",
                rc=proc.returncode,
                category=category,
            )
            if err.retryable and attempt <= self.retries:
                log.warning("%s; retrying in %.0fs", err, self.retry_delay)
                time.sleep(self.retry_delay)
                continue
            raise err

    # --------------------------------------------------------------- queries
    def version(self) -> str:
        return self.run(["--version"]).stdout.strip().split()[-1]

    def repository_info(self) -> RepositoryInfo:
        result = self.run(["info", "--json", "::"])
        return RepositoryInfo.from_json(json.loads(result.stdout))

    def list_archives(self, glob: str | None = None) -> list[ArchiveRef]:
        args = ["list", "--json"]
        if glob:
            args += ["--glob-archives", glob]
        args.append("::")
        data = json.loads(self.run(args).stdout)
        refs = []
        for entry in data.get("archives", []):
            try:
                refs.append(ArchiveRef.from_json(entry))
            except ValueError as exc:
                log.debug("skipping archive entry: %s", exc)
        return sorted(refs, key=lambda a: a.start)

    def archive_stats(self, archive: str) -> ArchiveStats:
        data = json.loads(self.run(["info", "--json", f"::{archive}"]).stdout)
        archives = data.get("archives") or []
        if not archives:
            raise BorgError(f"borg info returned no data for archive {archive}")
        return ArchiveStats.from_json(archives[0])

    def iter_items(self, archive: str, patterns: Iterable[str] = ()) -> Iterator[ArchiveItem]:
        """Stream ``borg list --json-lines`` for an archive (never materialises the list)."""
        args = self._base_args() + ["list", "--json-lines"]
        for pattern in patterns:
            args += ["--pattern", pattern]
        args.append(f"::{archive}")
        log.debug("borg list --json-lines ::%s", archive)
        proc = subprocess.Popen(
            args, env=self.environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    yield ArchiveItem.from_json(json.loads(line))
                except (ValueError, KeyError) as exc:
                    log.debug("unparseable list line: %s", exc)
        finally:
            _, stderr_bytes = proc.communicate()
            stderr = redact(stderr_bytes.decode("utf-8", "replace"))
            category = exit_code_category(proc.returncode)
            if category not in {"ok", "warning"}:
                raise BorgError(
                    f"borg list ::{archive} failed (rc={proc.returncode}, {category}): {stderr.strip()[-800:]}",
                    rc=proc.returncode,
                    category=category,
                )

    def cache_listing(self, archive: str, target: Path) -> Path:
        """Write the full item listing to a gzip'ed JSON-lines file for repeated local passes."""
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = target.with_suffix(target.suffix + ".part")
        count = 0
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            for item in self.iter_items(archive):
                fh.write(json.dumps(_item_to_json(item), separators=(",", ":")))
                fh.write("\n")
                count += 1
        os.chmod(tmp, 0o600)
        tmp.replace(target)
        log.debug("cached %d items of %s in %s", count, archive, target)
        return target

    # -------------------------------------------------------------- actions
    def extract(
        self,
        archive: str,
        dest_dir: str | Path,
        paths: Sequence[str] = (),
        patterns: Sequence[str] = (),
        strip_components: int = 0,
        dry_run: bool = False,
        numeric_ids: bool = False,
        timeout: int | None = None,
    ) -> BorgResult:
        """``borg extract`` into ``dest_dir`` (extraction is always relative to the cwd)."""
        args = ["extract"]
        if dry_run:
            args.append("--dry-run")
        if numeric_ids:
            args.append("--numeric-ids")
        if strip_components:
            args += ["--strip-components", str(strip_components)]
        for pattern in patterns:
            args += ["--pattern", pattern]
        args.append(f"::{archive}")
        args += list(paths)
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        return self.run(args, cwd=dest_dir, timeout=timeout)

    def extract_from_list(
        self,
        archive: str,
        dest_dir: str | Path,
        paths: Iterable[str],
        strip_components: int = 0,
        dry_run: bool = False,
        timeout: int | None = None,
    ) -> BorgResult:
        """Extract many exact paths via ``--patterns-from`` (avoids huge argv)."""
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        pattern_file = Path(dest_dir) / ".bbic-patterns"
        with open(pattern_file, "w", encoding="utf-8") as fh:
            for path in paths:
                fh.write(f"+ pf:{path}\n")
            fh.write("- sh:**\n")
        try:
            args = ["extract"]
            if dry_run:
                args.append("--dry-run")
            if strip_components:
                args += ["--strip-components", str(strip_components)]
            args += ["--patterns-from", str(pattern_file), f"::{archive}"]
            return self.run(args, cwd=dest_dir, timeout=timeout)
        finally:
            pattern_file.unlink(missing_ok=True)

    def check(
        self,
        archives_glob: str | None = None,
        last: int | None = None,
        repository_only: bool = False,
        archives_only: bool = False,
        verify_data: bool = False,
        max_duration: int | None = None,
        timeout: int | None = None,
    ) -> BorgResult:
        args = ["check"]
        if repository_only:
            args.append("--repository-only")
        if archives_only:
            args.append("--archives-only")
        if verify_data:
            args.append("--verify-data")
        if max_duration and repository_only:
            args += ["--max-duration", str(max_duration)]
        if archives_glob:
            args += ["--glob-archives", archives_glob]
        if last:
            args += ["--last", str(last)]
        args.append("::")
        return self.run(args, timeout=timeout)

    # -------------------------------------------------------------- helpers
    @staticmethod
    def find_binary(candidates: Iterable[str] = ()) -> str | None:
        for candidate in candidates:
            if candidate and os.access(candidate, os.X_OK):
                return candidate
        return shutil.which("borg")


def _item_to_json(item: ArchiveItem) -> dict[str, Any]:
    return {
        "path": item.path,
        "type": item.type,
        "size": item.size,
        "mtime": item.mtime.isoformat() if item.mtime else None,
        "mode": item.mode,
        "user": item.user,
        "group": item.group,
        "uid": item.uid,
        "gid": item.gid,
        "healthy": item.healthy,
        "linktarget": item.linktarget,
    }


def iter_cached_listing(path: Path) -> Iterator[ArchiveItem]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield ArchiveItem.from_json(json.loads(line))

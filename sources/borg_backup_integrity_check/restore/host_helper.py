"""On-host helper executed on the disposable restore server (Python 3.11+, stdlib + this package only).

The production server copies the whole package to ``/root/bbic/pkg`` and invokes
``python3 -m borg_backup_integrity_check.restore.host_helper <command> [--spec file]``.
Every command prints one JSON document on stdout. Borg credentials are read from
``/root/bbic/borg.env`` (0600) and never appear in argv.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BBIC_DIR = Path("/root/bbic")
BORG_ENV_FILE = BBIC_DIR / "borg.env"
WORK_ROOT = Path("/home/yunohost.backup/bbic-work")
ARCHIVES_DIR = Path("/home/yunohost.backup/archives")
YNH_SETTINGS = Path("/etc/yunohost/settings.yml")


# ----------------------------------------------------------------------------- utils
def sh(
    cmd: list[str] | str,
    *,
    timeout: int = 3600,
    check: bool = False,
    env: dict | None = None,
    cwd: str | None = None,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess:
    if isinstance(cmd, str):
        cmd = ["bash", "-c", cmd]
    proc = subprocess.run(
        cmd, capture_output=True, timeout=timeout, env=env, cwd=cwd, input=stdin, check=False
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd[:3])} failed (rc={proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[-1500:]}"
        )
    return proc


def out(data: dict) -> None:
    print(json.dumps(data, default=str))


def borg_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BORG_")}
    if BORG_ENV_FILE.is_file():
        for line in BORG_ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    env.setdefault("BORG_EXIT_CODES", "modern")
    env.setdefault("LANG", "C.UTF-8")
    return env


def borg_binary() -> str:
    env = borg_env()
    for candidate in (
        env.get("BBIC_BORG_BINARY"),
        "/var/www/borg/venv/bin/borg",
        shutil.which("borg"),
    ):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("no borg binary on the restore host")


def borg(
    args: list[str], *, cwd: str | None = None, timeout: int = 6 * 3600, stdin: bytes | None = None
) -> subprocess.CompletedProcess:
    lock_wait = borg_env().get("BBIC_LOCK_WAIT", "900")
    return sh(
        [borg_binary(), "--lock-wait", lock_wait] + args,
        env=borg_env(),
        cwd=cwd,
        timeout=timeout,
        stdin=stdin,
    )


def yunohost_json(args: list[str], timeout: int = 3 * 3600) -> tuple[int, dict | list | None, str]:
    proc = sh(["yunohost"] + args + ["--output-as", "json"], timeout=timeout)
    text = proc.stdout.decode("utf-8", "replace")
    data = None
    try:
        data = json.loads(text) if text.strip() else None
    except ValueError:
        data = None
    return proc.returncode, data, proc.stderr.decode("utf-8", "replace")


def reopen_port(port: int) -> None:
    """Keep the maintenance sshd reachable after YunoHost regenerated nftables from restored firewall.yml."""
    if not port:
        return
    proc = sh(
        [
            "yunohost",
            "firewall",
            "open",
            str(port),
            "--protocol",
            "tcp",
            "borg-backup-integrity-check",
        ],
        timeout=300,
    )
    if proc.returncode != 0:
        sh(["yunohost", "firewall", "allow", "TCP", str(port)], timeout=300)


def du_bytes(path: Path) -> int:
    proc = sh(["du", "-sb", str(path)], timeout=3600)
    try:
        return int(proc.stdout.decode().split()[0])
    except (IndexError, ValueError):
        return 0


# ---------------------------------------------------------------------- commands
def cmd_probe(_: argparse.Namespace) -> dict:
    info: dict = {"hostname": os.uname().nodename, "python": sys.version.split()[0]}
    try:
        info["os_release"] = dict(
            line.split("=", 1)
            for line in Path("/etc/os-release").read_text().splitlines()
            if "=" in line
        )
    except OSError:
        info["os_release"] = {}
    info["yunohost_installed"] = Path("/usr/bin/yunohost").exists()
    info["yunohost_postinstalled"] = Path("/etc/yunohost/installed").exists()
    info["cloud_init_done"] = Path("/var/lib/cloud/instance/boot-finished").exists()
    st = os.statvfs("/")
    info["disk_free_bytes"] = st.f_bavail * st.f_frsize
    info["disk_total_bytes"] = st.f_blocks * st.f_frsize
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, value = line.partition(":")
        mem[key] = int(value.split()[0]) * 1024 if value.strip() else 0
    info["mem_total_bytes"] = mem.get("MemTotal", 0)
    info["borg_binary"] = next(
        (
            c
            for c in ("/var/www/borg/venv/bin/borg", shutil.which("borg") or "")
            if c and os.access(c, os.X_OK)
        ),
        None,
    )
    return info


def cmd_borg_test(_: argparse.Namespace) -> dict:
    proc = borg(["list", "--json", "--last", "1", "::"], timeout=600)
    ok = proc.returncode == 0
    names = []
    if ok:
        try:
            names = [a["name"] for a in json.loads(proc.stdout.decode()).get("archives", [])]
        except ValueError:
            ok = False
    return {
        "ok": ok,
        "rc": proc.returncode,
        "archives": names,
        "error": None if ok else proc.stderr.decode("utf-8", "replace")[-800:],
    }


def cmd_wait_cloud_init(_: argparse.Namespace) -> dict:
    proc = sh(["cloud-init", "status", "--wait"], timeout=900)
    return {"rc": proc.returncode, "status": proc.stdout.decode("utf-8", "replace").strip()[-200:]}


def cmd_install_yunohost(ns: argparse.Namespace) -> dict:
    """Install YunoHost non-interactively (bookworm: install.yunohost.org, trixie: /trixie)."""
    if Path("/usr/bin/yunohost").exists():
        return {"ok": True, "skipped": True}
    url = (
        "https://install.yunohost.org/trixie"
        if ns.major == "13"
        else "https://install.yunohost.org"
    )
    distrib = ns.distrib or ("testing" if ns.major == "13" else "stable")
    script = sh(["curl", "-fsSL", url], timeout=300, check=True).stdout
    proc = sh(["bash", "-s", "--", "-a", "-d", distrib], stdin=script, timeout=3600)
    log_tail = (
        proc.stdout.decode("utf-8", "replace")[-3000:]
        + proc.stderr.decode("utf-8", "replace")[-3000:]
    )
    return {
        "ok": proc.returncode == 0 and Path("/usr/bin/yunohost").exists(),
        "rc": proc.returncode,
        "log_tail": log_tail,
    }


def cmd_install_borg_client(ns: argparse.Namespace) -> dict:
    """Install a Borg client before YunoHost is post-installed (borg_ynh needs a post-installed system).

    Preferred: the exact production version in a venv (like borg_ynh does); fallback: Debian's borgbackup.
    """
    venv = Path("/opt/bbic-borg")
    wanted = (ns.borg_version or "").strip()
    binary = venv / "bin" / "borg"
    if binary.exists():
        proc = sh([str(binary), "--version"], timeout=60)
        if not wanted or wanted in proc.stdout.decode(errors="replace"):
            return {"ok": True, "binary": str(binary), "skipped": True}
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    sh(["apt-get", "update", "-q"], timeout=900, env=env)
    deps = [
        "python3-venv",
        "python3-dev",
        "python3-pip",
        "build-essential",
        "pkg-config",
        "libssl-dev",
        "libacl1-dev",
        "liblz4-dev",
        "libzstd-dev",
        "libxxhash-dev",
        "libfuse3-dev",
        "fuse3",
        "borgbackup",
    ]
    apt = sh(
        ["apt-get", "install", "-y", "-q", "--no-install-recommends"] + deps, timeout=1800, env=env
    )
    result: dict = {"apt_rc": apt.returncode}
    if wanted:
        sh(["python3", "-m", "venv", "--upgrade", str(venv)], timeout=300)
        pip = sh(
            [
                str(venv / "bin" / "python3"),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--upgrade",
                "pip",
                "setuptools",
                "wheel",
            ],
            timeout=900,
        )
        pip = sh(
            [
                str(venv / "bin" / "python3"),
                "-m",
                "pip",
                "install",
                "--quiet",
                f"borgbackup=={wanted}",
            ],
            timeout=3600,
        )
        if pip.returncode == 0 and binary.exists():
            # `borg mount` needs FUSE bindings; without them the large-data mount falls back to
            # sampled extraction, so a failure here is not fatal.
            fuse = sh(
                [str(venv / "bin" / "python3"), "-m", "pip", "install", "--quiet", "pyfuse3"],
                timeout=1800,
            )
            result["fuse"] = fuse.returncode == 0
            result.update({"ok": True, "binary": str(binary), "version": wanted})
            _set_env_value("BBIC_BORG_BINARY", str(binary))
            return result
        result["pip_error"] = pip.stderr.decode("utf-8", "replace")[-800:]
    system = shutil.which("borg")
    if system:
        version = sh([system, "--version"], timeout=60).stdout.decode(errors="replace").strip()
        result.update({"ok": True, "binary": system, "version": version, "fallback": True})
        _set_env_value("BBIC_BORG_BINARY", system)
        return result
    result.update({"ok": False, "error": "no borg client could be installed"})
    return result


def _set_env_value(key: str, value: str) -> None:
    if not BORG_ENV_FILE.is_file():
        return
    lines = [
        line
        for line in BORG_ENV_FILE.read_text(encoding="utf-8").splitlines()
        if not line.startswith(key + "=")
    ]
    lines.append(f"{key}={value}")
    BORG_ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    BORG_ENV_FILE.chmod(0o600)


def cmd_install_borg_app(ns: argparse.Namespace) -> dict:
    """Install upstream borg_ynh on the restore host with its timer disabled (never backs up the host)."""
    if Path("/etc/yunohost/apps/borg").is_dir():
        sh(["systemctl", "disable", "--now", "borg.timer"], timeout=60)
        return {"ok": True, "skipped": True}
    env = borg_env()
    passphrase = env.get("BORG_PASSPHRASE", "")
    repo = env.get("BORG_REPO", "")
    args = (
        f"repository={_q(repo)}&passphrase={_q(passphrase)}&conf=0&data=0&apps=exclude:borg"
        "&on_calendar=2099-01-01 00:00:00&mailalert=never"
    )
    proc = sh(
        ["yunohost", "app", "install", ns.source or "borg", "--force", "--args", args], timeout=3600
    )
    installed = Path("/etc/yunohost/apps/borg").is_dir()
    sh(["systemctl", "disable", "--now", "borg.timer"], timeout=60)
    if proc.returncode != 0 or not installed:
        tail = (
            proc.stdout.decode("utf-8", "replace")[-1500:]
            + proc.stderr.decode("utf-8", "replace")[-1500:]
        ).strip()
        return {
            "ok": False,
            "rc": proc.returncode,
            "log_tail": tail or f"rc={proc.returncode}, app dir present={installed}",
        }
    sh(["yunohost", "app", "setting", "borg", "pruning_enabled", "-v", "false"], timeout=120)
    if Path("/var/www/borg/venv/bin/borg").exists():
        _set_env_value("BBIC_BORG_BINARY", "/var/www/borg/venv/bin/borg")
    if ns.ssh_port:
        reopen_port(int(ns.ssh_port))
    return {"ok": True, "rc": proc.returncode}


def _q(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def cmd_quarantine(ns: argparse.Namespace) -> dict:
    """Isolate the restored system from production: DynDNS, outbound mail, borg timers, self-mapped domains."""
    actions: list[str] = []
    hosts = Path("/etc/hosts")
    text = hosts.read_text(encoding="utf-8") if hosts.is_file() else ""
    lines = [line for line in text.splitlines() if "# bbic" not in line]
    lines.append("127.0.0.1 dyndns.yunohost.org # bbic-quarantine")
    for domain in [d for d in (ns.domains or "").split(",") if d]:
        lines.append(f"127.0.0.1 {domain} # bbic-domain")
        lines.append(f"::1 {domain} # bbic-domain")
    hosts.write_text("\n".join(lines) + "\n", encoding="utf-8")
    actions.append("hosts")
    for path in ("/etc/cron.d/yunohost-dyndns",):
        if Path(path).exists():
            Path(path).unlink()
            actions.append(f"removed {path}")
    for key in (
        Path("/etc/yunohost/dyndns").glob("K*") if Path("/etc/yunohost/dyndns").is_dir() else []
    ):
        key.unlink()
        actions.append(f"removed {key.name}")
    if shutil.which("postconf"):
        sh(
            [
                "postconf",
                "-e",
                "default_transport = error:outbound mail disabled on integrity-check restore host",
                "relay_transport = error:outbound mail disabled on integrity-check restore host",
                "relayhost =",
            ],
            timeout=120,
        )
        sh(["systemctl", "restart", "postfix"], timeout=120)
        actions.append("postfix sink")
    for unit in ("borg.timer", "borg.service"):
        sh(["systemctl", "disable", "--now", unit], timeout=60)
    for app_dir in (
        Path("/etc/yunohost/apps").glob("borg*") if Path("/etc/yunohost/apps").is_dir() else []
    ):
        sh(["systemctl", "disable", "--now", f"{app_dir.name}.timer"], timeout=60)
    actions.append("borg timers disabled")
    if ns.ssh_port:
        reopen_port(int(ns.ssh_port))
        actions.append(f"firewall port {ns.ssh_port} open")
    return {"ok": True, "actions": actions}


def cmd_deploy_credentials(ns: argparse.Namespace) -> dict:
    """Write borg.env from a JSON spec on stdin (repo, passphrase, key, known_hosts, remote path)."""
    spec = json.load(sys.stdin)
    BBIC_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path = BBIC_DIR / "borg_key"
    key_path.write_text(spec["private_key"], encoding="utf-8")
    key_path.chmod(0o600)
    known = BBIC_DIR / "known_hosts"
    known.write_text(spec.get("known_hosts") or "", encoding="utf-8")
    known.chmod(0o600)
    rsh = f"ssh -i {key_path} -oIdentitiesOnly=yes -oBatchMode=yes -oUserKnownHostsFile={known} -oStrictHostKeyChecking=accept-new"
    lines = [
        f"BORG_REPO={spec['repository']}",
        f"BORG_PASSPHRASE={spec.get('passphrase', '')}",
        f"BORG_RSH={rsh}",
        "BORG_RELOCATED_REPO_ACCESS_IS_OK=yes",
        "BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK=no",
        f"BBIC_LOCK_WAIT={spec.get('lock_wait', 900)}",
    ]
    if spec.get("remote_path"):
        lines.append(f"BORG_REMOTE_PATH={spec['remote_path']}")
    if spec.get("borg_binary"):
        lines.append(f"BBIC_BORG_BINARY={spec['borg_binary']}")
    BORG_ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    BORG_ENV_FILE.chmod(0o600)
    return {"ok": True}


def cmd_postinstall(ns: argparse.Namespace) -> dict:
    if Path("/etc/yunohost/installed").exists():
        return {"ok": True, "skipped": True}
    import secrets

    password = secrets.token_urlsafe(24)
    proc = sh(
        [
            "yunohost",
            "tools",
            "postinstall",
            "--domain",
            ns.domain,
            "--username",
            "bbicadmin",
            "--fullname",
            "Integrity Check Admin",
            "--password",
            password,
            "--ignore-dyndns",
            "--force-diskspace",
        ],
        timeout=1800,
    )
    return {
        "ok": proc.returncode == 0,
        "rc": proc.returncode,
        "log_tail": proc.stderr.decode("utf-8", "replace")[-1500:],
    }


def cmd_add_domain(ns: argparse.Namespace) -> dict:
    rc, data, err = yunohost_json(["domain", "list"], timeout=300)
    existing = (data or {}).get("domains", []) if isinstance(data, dict) else []
    if ns.domain in existing:
        return {"ok": True, "skipped": True}
    proc = sh(["yunohost", "domain", "add", ns.domain], timeout=900)
    return {
        "ok": proc.returncode == 0,
        "rc": proc.returncode,
        "log_tail": proc.stderr.decode("utf-8", "replace")[-1000:],
    }


def cmd_restore_core(ns: argparse.Namespace) -> dict:
    """Extract the core of an archive (everything except large roots), rebuild a YunoHost tar, restore it.

    Spec (JSON on stdin)::
        {"archive": "...", "targets": {"system": [...], "apps": [...]},
         "large_roots": [{"archive_path": "...", "skeleton": [{"path": "...", "user": "...", "group": "...", "mode": "drwxr-x---"}]}],
         "ssh_port": 22022, "force": true}
    """
    spec = json.load(sys.stdin)
    archive = spec["archive"]
    targets = spec.get("targets", {})
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"bbic_{archive}")[:50]
    work = WORK_ROOT / name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, mode=0o700)
    result: dict = {"archive": archive, "name": name}
    # 1. Extract everything except the large roots (their directory items are kept as skeleton).
    patterns = core_patterns(spec.get("large_roots", []))
    pattern_file = work / ".patterns"
    pattern_file.write_text("\n".join(patterns) + ("\n" if patterns else ""), encoding="utf-8")
    args = ["extract", "--numeric-ids" if spec.get("numeric_ids") else "--noflags"]
    if patterns:
        args += ["--patterns-from", str(pattern_file)]
    args.append(f"::{archive}")
    proc = borg(args, cwd=str(work))
    pattern_file.unlink(missing_ok=True)
    if proc.returncode not in (0, 1) and proc.returncode < 100:
        shutil.rmtree(work, ignore_errors=True)
        return {
            "ok": False,
            "stage": "extract",
            "rc": proc.returncode,
            "error": proc.stderr.decode("utf-8", "replace")[-1500:],
        }
    result["extract_warnings"] = proc.returncode != 0
    # 2. Make sure skeleton dirs exist with the right ownership even if borg skipped them.
    materialise_skeleton(work, spec.get("large_roots", []))
    # 3. Rewrite info.json sizes to what is really in the tar (YunoHost checks free disk space against them).
    info_path = work / "info.json"
    if not info_path.is_file():
        shutil.rmtree(work, ignore_errors=True)
        return {"ok": False, "stage": "layout", "error": "info.json missing after extraction"}
    info = json.loads(info_path.read_text(encoding="utf-8"))
    sizes: dict = {"system": {}, "apps": {}}
    total = 0
    for app in info.get("apps") or {}:
        size = du_bytes(work / "apps" / app) if (work / "apps" / app).exists() else 0
        sizes["apps"][app] = size
        total += size
    system = info.get("system") or info.get("hooks") or {}
    for part in system:
        rel = part.replace("_", "/", 1)
        size = du_bytes(work / rel) if (work / rel).exists() else 0
        sizes["system"][part] = size
        total += size
    info["size"], info["size_details"] = total, sizes
    info["description"] = f"[integrity-check sparse copy] {info.get('description', '')}".strip()
    info_path.write_text(json.dumps(info), encoding="utf-8")
    result["sparse_size"] = total
    # 4. Build the tar exactly like YunoHost expects (info.json at the root, relative paths).
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    tar_path = ARCHIVES_DIR / f"{name}.tar"
    with tarfile.open(tar_path, "w") as tar:
        for entry in sorted(work.iterdir()):
            tar.add(entry, arcname=entry.name)
    shutil.copy2(info_path, ARCHIVES_DIR / f"{name}.info.json")
    shutil.rmtree(work, ignore_errors=True)
    # 5. Restore through YunoHost itself.
    args = ["backup", "restore", name]
    if targets.get("system"):
        args += ["--system"] + list(targets["system"])
    if targets.get("apps"):
        args += ["--apps"] + list(targets["apps"])
    if spec.get("force", True):
        args.append("--force")
    apt_before = apt_source_snapshot()
    rc, data, err = yunohost_json(args, timeout=int(spec.get("timeout", 3 * 3600)))
    if ns.ssh_port or spec.get("ssh_port"):
        reopen_port(int(ns.ssh_port or spec.get("ssh_port")))
    result.update({"ok": rc == 0, "rc": rc, "results": data, "log_tail": err[-3000:]})
    result["log"] = _restore_operation_log(targets, err)
    failed = rc != 0 or any(
        outcome not in ("Success", "Warning")
        for section in (data or {}).values()
        if isinstance(section, dict)
        for outcome in section.values()
    )
    if failed:
        # The restore host is destroyed minutes from now; the only chance to keep the reason.
        result["log_text"] = _operation_log_text(result["log"])
        result["apt_reverted"] = revert_apt_sources(apt_before)
    for extra in (tar_path, ARCHIVES_DIR / f"{name}.info.json"):
        extra.unlink(missing_ok=True)
    return result


def _apply_owner(path: Path, entry: dict) -> None:
    mode = entry.get("mode") or ""
    if len(mode) == 10:
        bits = 0
        for i, ch in enumerate(mode[1:]):
            if ch != "-":
                bits |= 1 << (8 - i)
        with contextlib.suppress(OSError):
            path.chmod(bits or 0o750)
    user, group = entry.get("user"), entry.get("group")
    if user or group:
        sh(["chown", f"{user or ''}:{group or ''}", str(path)], timeout=60)


def _restore_operation_log(targets: dict, stderr: str) -> str | None:
    """Operation log of the restore that just ran, identified by name, never by recency.

    The newest entry of `yunohost log list` is not reliably this restore (it lags behind by one
    operation), which used to attribute every component the previous component's log.
    """
    printed = re.search(r"yunohost log (?:share|display) (\S+)", stderr)
    apps = [str(a) for a in (targets.get("apps") or [])]
    wanted = [f"backup_restore_app-{apps[0]}"] if apps else ["backup_restore_system"]
    if printed:
        wanted.insert(0, printed.group(1))
    for name in wanted:
        found = _operation_log(name)
        if found:
            return found
    return None


def _operation_log_text(path: str | None, limit: int = 120_000) -> str | None:
    """Tail of a YunoHost operation log: the ``.log`` beside the metadata ``.yml``, else the ``.yml``."""
    if not path:
        return None
    candidates = [Path(path)]
    if path.endswith(".yml"):
        candidates.insert(0, Path(path[: -len(".yml")] + ".log"))
    for candidate in candidates:
        if candidate.is_file():
            with contextlib.suppress(OSError):
                text = candidate.read_text(encoding="utf-8", errors="replace")
                return text if len(text) <= limit else "[...truncated...]\n" + text[-limit:]
    return None


def _operation_log(match: str) -> str | None:
    rc, data, _ = yunohost_json(
        ["log", "list", "--limit", "25", "--with-suboperations"], timeout=120
    )
    try:
        operations = data["operation"] if isinstance(data, dict) else []
        for op in operations:
            if match in str(op.get("name", "")):
                return op.get("path") or op.get("name")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return None


def cmd_extract_payload(ns: argparse.Namespace) -> dict:
    """Extract sampled objects (or whole roots in full mode) directly into their live locations.

    Spec: {"archive": "...", "roots": [{"archive_path": "...", "live_path": "...", "objects": [...], "full": false}]}
    Each object: {"archive_path": "...", "live_path": "...", "kind": "file"|"git_repo"}
    """
    spec = json.load(sys.stdin)
    archive = spec["archive"]
    stage = WORK_ROOT / "stage" / re.sub(r"[^A-Za-z0-9_.-]", "_", archive)
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, mode=0o700)
    results: list[dict] = []
    for root in spec.get("roots", []):
        archive_root = root["archive_path"].rstrip("/")
        live_root = root["live_path"].rstrip("/")
        patterns = payload_patterns(root)
        pattern_file = stage / ".patterns"
        pattern_file.write_text("\n".join(patterns) + "\n", encoding="utf-8")
        proc = borg(
            ["extract", "--noflags", "--patterns-from", str(pattern_file), f"::{archive}"],
            cwd=str(stage),
        )
        pattern_file.unlink(missing_ok=True)
        root_result = {
            "archive_path": archive_root,
            "live_path": live_root,
            "rc": proc.returncode,
            "error": None,
            "objects": [],
        }
        if proc.returncode not in (0, 1) and proc.returncode < 100:
            root_result["error"] = proc.stderr.decode("utf-8", "replace")[-1000:]
            results.append(root_result)
            continue
        staged_root = stage / archive_root
        if root.get("full"):
            if staged_root.exists():
                _merge_tree(staged_root, Path(live_root))
            root_result["objects"].append(
                {
                    "archive_path": archive_root,
                    "live_path": live_root,
                    "extracted": staged_root.exists() or Path(live_root).exists(),
                }
            )
        else:
            if staged_root.exists():
                _merge_tree(staged_root, Path(live_root))
            for obj in root.get("objects", []):
                live = Path(obj["live_path"])
                exists = (
                    live.exists()
                    if obj.get("kind") != "git_repo"
                    else (live / ".git" / "HEAD").exists()
                )
                root_result["objects"].append(
                    {
                        "archive_path": obj["archive_path"],
                        "live_path": obj["live_path"],
                        "kind": obj.get("kind", "file"),
                        "extracted": exists,
                    }
                )
        results.append(root_result)
    shutil.rmtree(stage, ignore_errors=True)
    return {"ok": all(r["error"] is None for r in results), "roots": results}


APT_SOURCE_PATHS = (
    Path("/etc/apt/sources.list"),
    Path("/etc/apt/sources.list.d"),
    Path("/etc/apt/preferences.d"),
    Path("/etc/apt/keyrings"),
    Path("/etc/apt/trusted.gpg.d"),
)


def apt_source_snapshot(paths: tuple[Path, ...] = APT_SOURCE_PATHS) -> dict[str, bytes]:
    """Every apt source, pin and key file, by content: a file set, so any format is covered."""
    snapshot: dict[str, bytes] = {}
    for entry in paths:
        files = sorted(entry.iterdir()) if entry.is_dir() else [entry]
        for path in files:
            if path.is_file():
                with contextlib.suppress(OSError):
                    snapshot[str(path)] = path.read_bytes()
    return snapshot


def revert_apt_sources(
    before: dict[str, bytes], paths: tuple[Path, ...] = APT_SOURCE_PATHS
) -> list[str]:
    """Undo what a failed app restore left behind in apt's source directories.

    An app that adds a third-party repository and then fails leaves the repository configured.
    The next app's ``_ynh_apt update --error-on=any`` reads all of sources.list.d, so one broken
    mirror in a failed app's repository fails the next app too (immich -> jitsi, run ...-173222).
    A successful restore keeps its repositories: production has them as well.
    """
    changed: list[str] = []
    after = apt_source_snapshot(paths)
    for path, content in after.items():
        if path not in before:
            with contextlib.suppress(OSError):
                Path(path).unlink()
                changed.append(f"removed {path}")
        elif before[path] != content:
            with contextlib.suppress(OSError):
                Path(path).write_bytes(before[path])
                changed.append(f"reverted {path}")
    for path, content in before.items():
        if path not in after:
            with contextlib.suppress(OSError):
                Path(path).write_bytes(content)
                changed.append(f"restored {path}")
    return changed


def materialise_skeleton(work: Path, large_roots: list[dict]) -> None:
    """Recreate the excluded roots' directories with their archive ownership (borg may skip them).

    Entries marked ``kind: file`` or ``kind: subtree`` were extracted by the patterns with their
    own metadata - making a directory of their path would shadow what was extracted.
    """
    for root in large_roots:
        for entry in root.get("skeleton", []):
            if entry.get("kind") in ("file", "subtree"):
                continue
            path = work / entry["path"]
            path.mkdir(parents=True, exist_ok=True)
            _apply_owner(path, entry)


def core_patterns(large_roots: list[dict]) -> list[str]:
    """Borg patterns extracting everything but the large roots.

    A root's skeleton survives the exclusion: its directory items, and the small plumbing files
    the app's own restore script reads (borg takes the first matching pattern, so they come first).
    """
    patterns: list[str] = []
    for root in large_roots:
        for entry in root.get("skeleton", []):
            prefix = "pp" if entry.get("kind") == "subtree" else "pf"
            patterns.append(f"+ {prefix}:{entry['path']}")
        patterns.append(f"- pp:{root['archive_path']}")
    return patterns


def payload_patterns(root: dict) -> list[str]:
    """Borg patterns extracting one large root completely (full) or only its sampled objects.

    Sampled files come with the directory items of all their ancestors (inside the root) so that
    ownership and permissions of the recreated tree match the archive; git repositories only need
    their metadata files (HEAD, refs, logs) for evidence.
    """
    archive_root = root["archive_path"].rstrip("/")
    patterns: list[str] = []
    if root.get("full"):
        patterns.append(f"+ pp:{archive_root}")
    else:
        seen: set[str] = set()
        for obj in root.get("objects", []):
            ap = obj["archive_path"]
            if obj.get("kind") == "git_repo":
                for meta in ("HEAD", "logs/HEAD", "packed-refs", "config", "description"):
                    patterns.append(f"+ pf:{ap}/.git/{meta}")
                patterns.append(f"+ sh:{ap}/.git/refs/**")
                ancestors = f"{ap}/.git"
            else:
                patterns.append(f"+ pf:{ap}")
                ancestors = ap.rsplit("/", 1)[0]
            parts = ancestors.split("/")
            for i in range(len(parts), 0, -1):
                d = "/".join(parts[:i])
                if d in seen or not d.startswith(archive_root):
                    break
                seen.add(d)
                patterns.append(f"+ pf:{d}")
    patterns.append("- sh:**")
    return patterns


def _merge_tree(src: Path, dst: Path) -> None:
    """Copy a staged tree into place preserving ownership/permissions/times (cp -a semantics)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        sh(["cp", "-a", f"{src}/.", f"{dst}/"], timeout=6 * 3600, check=True)
    else:
        sh(["cp", "-a", str(src), str(dst)], timeout=6 * 3600, check=True)


MOUNT_ROOT = Path("/mnt/bbic")
MOUNTS_FILE = Path("/proc/mounts")


def _borg_candidates() -> list[str]:
    """Binaries to try for `borg mount`, best first: not every borg build has FUSE support."""
    seen: list[str] = []
    for candidate in (
        borg_env().get("BBIC_BORG_BINARY"),
        "/opt/bbic-borg/bin/borg",
        shutil.which("borg"),
    ):
        if candidate and candidate not in seen and Path(candidate).exists():
            seen.append(candidate)
    return seen


def _enable_metacopy() -> bool:
    """Metadata-only copy-up: without it, an app's `chown -R` copies the whole data set up."""
    sh(["modprobe", "overlay"], timeout=60)
    knob = Path("/sys/module/overlay/parameters/metacopy")
    if not knob.is_file():
        return False
    try:
        if knob.read_text().strip().upper().startswith("N"):
            knob.write_text("Y")
        return knob.read_text().strip().upper().startswith("Y")
    except OSError:
        return False


def _mount_archive(archive: str, target: Path) -> tuple[bool, str]:
    target.mkdir(parents=True, exist_ok=True)
    if os.path.ismount(target):
        return True, ""
    errors = []
    for binary in _borg_candidates():
        proc = sh(
            [
                binary,
                "--lock-wait",
                borg_env().get("BBIC_LOCK_WAIT", "900"),
                "mount",
                f"::{archive}",
                str(target),
            ],
            timeout=1800,
            env=borg_env(),
        )
        if proc.returncode == 0 and os.path.ismount(target):
            return True, binary
        errors.append(f"{binary}: {proc.stderr.decode('utf-8', 'replace').strip()[-200:]}")
    return False, "; ".join(errors) or "no borg binary available"


def cmd_mount_large_roots(ns: argparse.Namespace) -> dict:
    """Serve an archive's large data from the repository instead of extracting it.

    Spec: {"archive": "...", "roots": [{"archive_path": "...", "live_path": "..."}]}
    Each root becomes an overlay whose lower layer is the archive (read-only, fetched on demand)
    and whose upper layer is local disk, so the restored app can write.
    """
    spec = json.load(sys.stdin)
    archive = spec["archive"]
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", archive)[:60]
    mountpoint = MOUNT_ROOT / "archives" / name
    ok, detail = _mount_archive(archive, mountpoint)
    if not ok:
        return {"ok": False, "error": f"borg mount failed ({detail})"}
    metacopy = _enable_metacopy()
    mounted, errors = [], []
    roots = sorted(spec.get("roots", []), key=lambda r: len(Path(r["live_path"]).parts))
    for index, root in enumerate(roots):
        lower = mountpoint / root["archive_path"]
        live = Path(root["live_path"])
        if not lower.is_dir():
            errors.append(f"{root['archive_path']}: not a directory in the archive")
            continue
        upper = MOUNT_ROOT / "upper" / name / str(index)
        work = MOUNT_ROOT / "work" / name / str(index)
        for path in (upper, work, live):
            path.mkdir(parents=True, exist_ok=True)
        options = f"lowerdir={lower},upperdir={upper},workdir={work}"
        proc = sh(
            [
                "mount",
                "-t",
                "overlay",
                "bbic-overlay",
                "-o",
                options + (",metacopy=on" if metacopy else ""),
                str(live),
            ],
            timeout=300,
        )
        if proc.returncode != 0 and metacopy:
            proc = sh(
                ["mount", "-t", "overlay", "bbic-overlay", "-o", options, str(live)], timeout=300
            )
        if proc.returncode != 0:
            errors.append(
                f"{root['live_path']}: {proc.stderr.decode('utf-8', 'replace').strip()[-200:]}"
            )
            continue
        mounted.append(root["live_path"])
    return {
        "ok": bool(mounted) and not errors,
        "mounted": mounted,
        "errors": errors,
        "metacopy": metacopy,
        "archive_mountpoint": str(mountpoint),
    }


def cmd_unmount_large_roots(ns: argparse.Namespace) -> dict:
    """Unmount every overlay and archive mount, so the host can be destroyed cleanly."""
    actions = []
    for line in reversed(MOUNTS_FILE.read_text(encoding="utf-8").splitlines()):
        fields = line.split()
        if len(fields) < 3:
            continue
        device, target, fstype = fields[0], fields[1].replace("\\040", " "), fields[2]
        if device == "bbic-overlay" or (
            fstype.startswith("fuse") and target.startswith(str(MOUNT_ROOT))
        ):
            if sh(["umount", target], timeout=300).returncode != 0:
                sh(["umount", "-l", target], timeout=300)
            actions.append(target)
    return {"ok": True, "unmounted": actions}


def cmd_describe(ns: argparse.Namespace) -> dict:
    """Run the generic evidence extractor on the host for every object in the spec."""
    from ..evidence.extractors import EvidenceExtractor

    spec = json.load(sys.stdin)
    extractor = EvidenceExtractor(include_sender=spec.get("include_sender", True))
    results = []
    for obj in spec.get("objects", []):
        path = Path(obj["live_path"])
        mtime = None
        if obj.get("mtime"):
            from datetime import datetime

            try:
                mtime = datetime.fromisoformat(obj["mtime"])
            except ValueError:
                mtime = None
        ev = extractor.describe(
            path,
            kind_hint=obj.get("kind", "file"),
            mtime=mtime,
            size=obj.get("size"),
            display_path=obj.get("relative_path"),
        )
        data = ev.to_dict()
        data["details"]["relative_path"] = obj.get("relative_path")
        results.append({"archive_path": obj.get("archive_path"), "evidence": data})
    return {"ok": True, "objects": results}


def cmd_health(ns: argparse.Namespace) -> dict:
    """Service / HTTP / database checks for one app (spec on stdin)."""
    spec = json.load(sys.stdin)
    result: dict = {"services": [], "http": None, "db": None, "db_refs": [], "sso": None}
    for service in spec.get("services", []):
        proc = sh(["systemctl", "is-active", service], timeout=60)
        state = proc.stdout.decode().strip()
        if state in ("active", "inactive", "failed", "activating"):
            result["services"].append({"name": service, "state": state})
    result["registered_services"] = _yunohost_services_for(spec.get("app"))
    domain, path = spec.get("domain"), spec.get("path") or "/"
    if domain:
        result["http"] = _http_probe(
            f"https://{domain}{path if path.endswith('/') else path + '/'}", domain
        )
        result["sso"] = _http_probe(f"https://{domain}/yunohost/sso/", domain)
        for extra in spec.get("urls", []):
            result.setdefault("extra_urls", []).append(_http_probe(extra, domain))
    db_type, db_name = spec.get("db_type"), spec.get("db_name")
    if db_type and db_name:
        result["db"] = _db_probe(db_type, db_name)
        names = [n for n in spec.get("basenames", []) if len(n) >= 6]
        if names and result["db"].get("ok"):
            result["db_refs"] = _db_references(db_type, db_name, names)
    return result


def _yunohost_services_for(app: str | None) -> list[dict]:
    if not app:
        return []
    rc, data, _ = yunohost_json(["service", "status"], timeout=300)
    found = []
    if isinstance(data, dict):
        for name, info in data.items():
            if name == app or name.startswith(app + "-") or name.startswith(app + "_"):
                found.append(
                    {
                        "name": name,
                        "status": info.get("status"),
                        "configuration": info.get("configuration"),
                    }
                )
    return found


def _http_probe(url: str, domain: str) -> dict:
    proc = sh(
        [
            "curl",
            "-k",
            "-s",
            "-o",
            "/dev/null",
            "-m",
            "60",
            "-w",
            "%{http_code} %{redirect_url}",
            "--resolve",
            f"{domain}:443:127.0.0.1",
            "--resolve",
            f"{domain}:80:127.0.0.1",
            url,
        ],
        timeout=90,
    )
    text = proc.stdout.decode("utf-8", "replace").strip()
    code, _, redirect = text.partition(" ")
    return {
        "url": url,
        "code": int(code) if code.isdigit() else 0,
        "redirect": redirect or None,
        "rc": proc.returncode,
    }


def _db_probe(db_type: str, db_name: str) -> dict:
    if db_type == "postgresql":
        proc = sh(
            [
                "sudo",
                "-u",
                "postgres",
                "psql",
                "-tA",
                "-d",
                db_name,
                "-c",
                "select count(*) from pg_tables where schemaname='public'",
            ],
            timeout=120,
        )
    else:
        proc = sh(
            [
                "mysql",
                "-N",
                "-B",
                "-e",
                f"select count(*) from information_schema.tables where table_schema='{db_name}'",
            ],
            timeout=120,
        )
    text = proc.stdout.decode().strip()
    try:
        tables = int(text.splitlines()[-1]) if text else 0
    except ValueError:
        tables = 0
    return {
        "ok": proc.returncode == 0,
        "tables": tables,
        "error": None if proc.returncode == 0 else proc.stderr.decode("utf-8", "replace")[-300:],
    }


def _db_references(db_type: str, db_name: str, names: list[str]) -> list[str]:
    with tempfile.NamedTemporaryFile("w", delete=False, prefix="bbic-names") as fh:
        fh.write("\n".join(names) + "\n")
        names_file = fh.name
    try:
        if db_type == "postgresql":
            dump = "sudo -u postgres pg_dump --data-only " + shlex.quote(db_name)
        else:
            dump = "mysqldump --no-create-info --skip-triggers --skip-comments " + shlex.quote(
                db_name
            )
        proc = sh(
            f"{dump} 2>/dev/null | grep -aoF -f {shlex.quote(names_file)} | sort -u", timeout=1800
        )
        return sorted(
            {
                line.strip()
                for line in proc.stdout.decode("utf-8", "replace").splitlines()
                if line.strip()
            }
        )
    finally:
        os.unlink(names_file)


def cmd_mail_verify(ns: argparse.Namespace) -> dict:
    """Check through Dovecot (doveadm) that sampled messages are visible in the user's mailbox."""
    spec = json.load(sys.stdin)
    results = []
    if not shutil.which("doveadm"):
        return {"ok": False, "error": "doveadm not available", "objects": []}
    # Files copied straight into the maildirs (new/) are only picked up after a force-resync;
    # a plain index or search on its own returns nothing for restored messages.
    for user in sorted({o.get("user") for o in spec.get("objects", []) if o.get("user")}):
        for maildir in (Path("/var/mail") / user,):
            for sub in ("cur", "new", "tmp"):
                (maildir / sub).mkdir(parents=True, exist_ok=True)
            sh(["chown", "-R", "vmail:mail", str(maildir)], timeout=120)
        sh(["doveadm", "force-resync", "-u", user, "*"], timeout=600)
    for obj in spec.get("objects", []):
        message_id, user = obj.get("message_id"), obj.get("user")
        verified = False
        detail = ""
        if message_id and user:
            needle = message_id.strip().strip("<>")
            proc = sh(
                ["doveadm", "search", "-u", user, "HEADER", "Message-ID", needle], timeout=120
            )
            verified = proc.returncode == 0 and bool(proc.stdout.strip())
            detail = proc.stderr.decode("utf-8", "replace")[-200:] if not verified else ""
        results.append(
            {"archive_path": obj.get("archive_path"), "verified": verified, "detail": detail}
        )
    return {"ok": True, "objects": results}


def cmd_app_settings(ns: argparse.Namespace) -> dict:
    """Read a restored app's settings (domain/path/db) on the host."""
    path = Path("/etc/yunohost/apps") / ns.app / "settings.yml"
    if not path.is_file():
        return {"ok": False}
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    keys = ("domain", "path", "db_name", "install_dir", "data_dir", "id")
    return {"ok": True, "settings": {k: data.get(k) for k in keys}}


def cmd_domains(ns: argparse.Namespace) -> dict:
    rc, data, _ = yunohost_json(["domain", "list"], timeout=300)
    return {
        "ok": rc == 0,
        "domains": (data or {}).get("domains", []) if isinstance(data, dict) else [],
    }


def cmd_reopen_port(ns: argparse.Namespace) -> dict:
    reopen_port(int(ns.port))
    return {"ok": True}


def cmd_mount_volume(ns: argparse.Namespace) -> dict:
    """Format (if needed) and mount an attached volume at /home before anything is restored."""
    device = ns.device
    for _ in range(30):
        if Path(device).exists():
            break
        sh(["sleep", "2"])
    if not Path(device).exists():
        return {"ok": False, "error": f"device {device} not found"}
    proc = sh(["blkid", "-o", "value", "-s", "TYPE", device], timeout=60)
    if not proc.stdout.strip():
        sh(["mkfs.ext4", "-q", "-F", device], timeout=1800, check=True)
    mountpoint = ns.mountpoint or "/home"
    tmp = Path("/mnt/bbic-volume")
    tmp.mkdir(parents=True, exist_ok=True)
    sh(["mount", device, str(tmp)], timeout=120, check=True)
    if Path(mountpoint).exists():
        sh(["cp", "-a", f"{mountpoint}/.", f"{tmp}/"], timeout=1800, check=True)
    sh(["umount", str(tmp)], timeout=120, check=True)
    Path(mountpoint).mkdir(parents=True, exist_ok=True)
    sh(["mount", device, mountpoint], timeout=120, check=True)
    with open("/etc/fstab", "a", encoding="utf-8") as fh:
        fh.write(f"{device} {mountpoint} ext4 defaults,nofail 0 2\n")
    for extra in ("/var/mail",):
        target = Path(mountpoint) / ".bbic-var-mail"
        target.mkdir(parents=True, exist_ok=True)
        if Path(extra).exists():
            sh(["cp", "-a", f"{extra}/.", f"{target}/"], timeout=1800)
        Path(extra).mkdir(parents=True, exist_ok=True)
        sh(["mount", "--bind", str(target), extra], timeout=60)
    return {"ok": True, "mountpoint": mountpoint}


COMMANDS = {
    "probe": cmd_probe,
    "borg-test": cmd_borg_test,
    "wait-cloud-init": cmd_wait_cloud_init,
    "install-yunohost": cmd_install_yunohost,
    "install-borg-client": cmd_install_borg_client,
    "install-borg-app": cmd_install_borg_app,
    "deploy-credentials": cmd_deploy_credentials,
    "quarantine": cmd_quarantine,
    "postinstall": cmd_postinstall,
    "add-domain": cmd_add_domain,
    "restore-core": cmd_restore_core,
    "extract-payload": cmd_extract_payload,
    "mount-large-roots": cmd_mount_large_roots,
    "unmount-large-roots": cmd_unmount_large_roots,
    "describe": cmd_describe,
    "health": cmd_health,
    "mail-verify": cmd_mail_verify,
    "app-settings": cmd_app_settings,
    "domains": cmd_domains,
    "reopen-port": cmd_reopen_port,
    "mount-volume": cmd_mount_volume,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bbic-host-helper")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--major", default="12")
    parser.add_argument("--distrib", default=None)
    parser.add_argument("--source", default=None)
    parser.add_argument("--domains", default="")
    parser.add_argument("--ssh-port", default=None)
    parser.add_argument("--domain", default=None)
    parser.add_argument("--app", default=None)
    parser.add_argument("--port", default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--mountpoint", default=None)
    parser.add_argument("--borg-version", default=None)
    ns = parser.parse_args(argv)
    try:
        out(COMMANDS[ns.command](ns))
        return 0
    except Exception as exc:  # noqa: BLE001 - report every failure as JSON for the orchestrator
        out({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())

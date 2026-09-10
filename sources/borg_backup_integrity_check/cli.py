"""``borg-backup-integrity-check`` command line interface (operational front-end)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import APP_ID, __version__
from .config import AppConfig, Paths
from .errors import ConfigurationError, IntegrityCheckError
from .logging_setup import get_logger, setup_logging
from .redaction import redact
from .secrets import KNOWN_SECRETS, SecretStore

log = get_logger("cli")


def _require_root() -> None:
    if os.geteuid() != 0:
        raise ConfigurationError(
            "this command must run as root (try: sudo borg-backup-integrity-check ...)"
        )


def _load_config() -> AppConfig:
    return AppConfig.load(Paths.from_environment())


def _out(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=1, default=str))
    elif isinstance(data, str):
        print(data)
    else:
        for key, value in data.items() if isinstance(data, dict) else enumerate(data):
            print(f"{key}: {value}")


# ----------------------------------------------------------------------- run
def cmd_run(ns: argparse.Namespace) -> int:
    from .run.orchestrator import IntegrityRun, RunOptions

    _require_root()
    config = _load_config()
    mode = ns.mode or str(config.restore_mode)
    retain = None
    if ns.retain is not None:
        retain = float(ns.retain) if ns.retain else float(config.retain_hours)
    options = RunOptions(
        mode=mode,
        retain_hours=retain,
        provider_override=ns.provider,
        components=ns.component or None,
        inspect_only=ns.inspect_only,
        email=ns.email or ns.scheduled,
        quiet=ns.quiet,
        keep_host_on_failure=ns.keep_host_on_failure if ns.keep_host_on_failure else None,
    )
    run = IntegrityRun(config, options)
    setup_logging(config.paths.log_dir / f"{run.run_id}.log", verbose=ns.verbose, quiet=ns.quiet)
    report = run.run()
    if ns.json:
        print(json.dumps(report.to_dict(), indent=1, default=str))
    elif not ns.quiet or not ns.scheduled:
        print()
        print((config.paths.runs_dir / run.run_id / "report.txt").read_text(encoding="utf-8"))
    return 0 if report.overall != "FAIL" else 1


# -------------------------------------------------------------------- status
def cmd_status(ns: argparse.Namespace) -> int:
    from .run.state import RunStateStore

    _require_root()
    config = _load_config()
    store = RunStateStore(config.paths.runs_dir)
    latest = store.latest()
    data = {
        "app": config.app_id,
        "provider": config.provider_name,
        "restore_mode": config.restore_mode,
        "schedule": config.on_calendar() if config.schedule_enabled else "disabled",
        "latest_run": latest.to_dict() if latest else None,
        "unfinished_runs": [s.run_id for s in store.unfinished()],
    }
    if ns.json:
        _out(data, True)
        return 0
    if latest is None:
        print("No integrity check has run yet.")
    else:
        print(f"Latest run:      {latest.run_id} ({latest.mode}, {latest.provider})")
        print(f"Started:         {latest.started_at:%Y-%m-%d %H:%M}")
        print(f"Status:          {latest.status} / phase {latest.phase}")
        print(f"Result:          {latest.overall or 'n/a'}")
        print(
            f"Cleanup:         {latest.cleanup_status}"
            + (f" ({latest.cleanup_error})" if latest.cleanup_error else "")
        )
        if latest.retained_until:
            print(
                f"Retained host:   {latest.host_address}:{latest.host_ssh_port} until {latest.retained_until:%Y-%m-%d %H:%M}"
            )
        if latest.report_path:
            print(f"Report:          {latest.report_path}")
    unfinished = store.unfinished()
    if unfinished:
        print(f"Runs needing attention: {', '.join(s.run_id for s in unfinished)} (run 'cleanup')")
    print(f"Schedule:        {data['schedule']}")
    return 0


def cmd_history(ns: argparse.Namespace) -> int:
    from .manifest.history import HistoryStore
    from .run.state import RunStateStore
    from .units import format_bytes, format_count

    _require_root()
    config = _load_config()
    manifests = HistoryStore(config.paths.history_dir).load_all()[-ns.limit :]
    runs = RunStateStore(config.paths.runs_dir).list()[-ns.limit :]
    if ns.json:
        _out(
            {"manifests": [m.to_dict() for m in manifests], "runs": [r.to_dict() for r in runs]},
            True,
        )
        return 0
    print("Backup manifests (newest last):")
    print(f"{'Backup time':<18}{'Logical size':>14}{'Objects':>12}{'Components':>12}")
    for m in manifests:
        print(
            f"{m.backup_time:%Y-%m-%d %H:%M}  {format_bytes(m.total_logical):>14}{format_count(m.total_files):>12}{len(m.archives):>12}"
        )
    print()
    print("Integrity runs:")
    for r in runs:
        print(
            f"{r.started_at:%Y-%m-%d %H:%M}  {r.run_id}  {r.mode:<8}{r.status:<12}{r.overall or '':<20}cleanup={r.cleanup_status}"
        )
    return 0


def cmd_report(ns: argparse.Namespace) -> int:
    from .run.state import RunStateStore

    _require_root()
    config = _load_config()
    store = RunStateStore(config.paths.runs_dir)
    state = store.load(ns.run_id) if ns.run_id else store.latest()
    if state is None or not state.report_path or not Path(state.report_path).is_file():
        print("no report available", file=sys.stderr)
        return 1
    print(Path(state.report_path).read_text(encoding="utf-8"))
    return 0


# ------------------------------------------------------------------ cleanup
def _manager(config: AppConfig, provider_name: str | None = None):
    from .providers.registry import create_provider
    from .run.cleanup import LifecycleManager
    from .run.state import RunStateStore

    provider = create_provider(
        provider_name or config.provider_name, config.credentials(), config.provider_settings
    )
    return LifecycleManager(provider, RunStateStore(config.paths.runs_dir), config.owner_id)


def cmd_destroy(ns: argparse.Namespace) -> int:
    from .run.state import RunStateStore

    _require_root()
    config = _load_config()
    store = RunStateStore(config.paths.runs_dir)
    targets = (
        store.unfinished()
        if ns.all
        else [
            s for s in [store.load(ns.run_id) if ns.run_id else _latest_with_resources(store)] if s
        ]
    )
    if not targets:
        print("nothing to destroy (no run with live cloud resources)")
        return 0
    rc = 0
    for state in targets:
        try:
            manager = _manager(config, state.provider)
            result = manager.destroy_run(state, reason="manual destroy")
            print(f"{state.run_id}: destroyed {', '.join(result.destroyed) or 'nothing'}")
        except IntegrityCheckError as exc:
            print(f"{state.run_id}: CLEANUP FAILED: {redact(str(exc))}", file=sys.stderr)
            rc = 1
    return rc


def _latest_with_resources(store):
    for state in reversed(store.list()):
        if state.has_cloud_resources:
            return state
    return None


def cmd_cleanup(ns: argparse.Namespace) -> int:
    from .run.cleanup import resources_summary

    _require_root()
    config = _load_config()
    manager = _manager(config, ns.provider)
    expired = manager.expire_retained()
    for rep in expired:
        print("expired retained host destroyed: " + ", ".join(rep.destroyed))
    stale = manager.find_stale()
    if not stale:
        print(
            "no stale resources found for this server"
            + (" (expired runs handled)" if expired else "")
        )
        return 0
    print(f"stale resources: {resources_summary(stale)}")
    if ns.dry_run:
        print("dry run: nothing destroyed")
        return 0
    if not ns.yes and sys.stdin.isatty():
        answer = input("Destroy these resources? [y/N] ")
        if answer.strip().lower() != "y":
            return 0
    result = manager.destroy_resources(stale)
    for item in result.destroyed:
        print(f"destroyed {item}")
    for item in result.failed:
        print(f"FAILED {item}", file=sys.stderr)
    return 0 if result.ok else 1


# --------------------------------------------------------------- test-config
def cmd_test_config(ns: argparse.Namespace) -> int:
    from .borg.client import BorgClient
    from .errors import BorgError, ProviderError
    from .providers.registry import create_provider

    _require_root()
    config = _load_config()
    results: dict[str, dict] = {}
    if ns.what in ("borg", "all"):
        try:
            source = config.borg_source()
            client = BorgClient(
                source.binary,
                source.repository,
                source.passphrase,
                source.ssh_key_path,
                remote_path=source.remote_path,
                lock_wait=60,
                retries=0,
            )
            archives = client.list_archives()
            newest = archives[-1] if archives else None
            results["borg"] = {
                "ok": True,
                "detail": f"{len(archives)} archive(s); newest {newest.name if newest else 'none'} ({newest.start:%Y-%m-%d %H:%M})"
                if newest
                else f"{len(archives)} archive(s)",
                "source": source.describe(),
            }
        except (BorgError, ConfigurationError) as exc:
            results["borg"] = {"ok": False, "detail": redact(str(exc))}
    if ns.what in ("provider", "all"):
        try:
            provider = create_provider(
                ns.provider or config.provider_name, config.credentials(), config.provider_settings
            )
            detail = provider.validate_credentials()
            region = str(
                config.hetzner_location
                if provider.name == "hetzner"
                else config.digitalocean_region
            )
            regions = (
                {r.id for r in provider.list_regions()} if provider.capabilities.regions else set()
            )
            if regions and region not in regions:
                results["provider"] = {
                    "ok": False,
                    "detail": f"{detail}; region {region!r} unknown (valid: {', '.join(sorted(regions))})",
                }
            else:
                preferred = str(
                    config.hetzner_server_type
                    if provider.name == "hetzner"
                    else config.digitalocean_size
                )
                try:
                    size = provider.choose_size(
                        region, int(config.vm_min_memory_mb), 0, preferred=preferred
                    )
                    detail += (
                        f"; size {size.id} ({size.memory_mb} MB, {size.disk_gb} GB) in {region}"
                    )
                    results["provider"] = {"ok": True, "detail": detail}
                except LookupError as exc:
                    results["provider"] = {"ok": False, "detail": f"{detail}; {exc}"}
        except (ProviderError, ConfigurationError) as exc:
            results["provider"] = {"ok": False, "detail": redact(str(exc))}
    if ns.json:
        _out(results, True)
    else:
        for name, res in results.items():
            print(f"{name}: {'OK' if res['ok'] else 'FAILED'} - {res['detail']}")
    return 0 if all(r["ok"] for r in results.values()) else 1


def cmd_catalog(ns: argparse.Namespace) -> int:
    """Regions/sizes for the config panel (dynamic choices), cached for a few hours."""
    from .providers.catalog import catalog_data, ynh_choices_yaml

    _require_root()
    config = _load_config()
    provider_name = ns.provider or config.provider_name
    region = ns.region or str(
        config.hetzner_location if provider_name == "hetzner" else config.digitalocean_region
    )
    data = catalog_data(config, provider_name, ns.what, region, cache_dir=config.paths.cache_dir)
    if ns.ynh_choices:
        print(ynh_choices_yaml(data.get(ns.what, []), ns.current or "", ns.extra))
        return 0
    _out(data, True)
    return 0 if "error" not in data else 1


# ------------------------------------------------------------------- secrets
def cmd_secret(ns: argparse.Namespace) -> int:
    _require_root()
    paths = Paths.from_environment()
    store = SecretStore(paths.secrets_file)
    if ns.action == "status":
        names = [ns.name] if ns.name else list(KNOWN_SECRETS)
        data = {name: store.status(name) for name in names}
        if ns.json:
            _out(data, True)
        else:
            for name, status in data.items():
                print(
                    f"{name}: {'configured (' + status['fingerprint'] + ')' if status['configured'] else 'not configured'}"
                )
        return 0
    if not ns.name:
        print("secret name required", file=sys.stderr)
        return 2
    if ns.action == "delete":
        print("deleted" if store.delete(ns.name) else "not set")
        return 0
    value = (
        sys.stdin.read().rstrip("\n")
        if ns.stdin or not sys.stdin.isatty()
        else os.environ.get("BBIC_SECRET_VALUE", "")
    )
    if not value:
        if ns.allow_empty:
            print("empty value: existing secret left unchanged")
            return 0
        print("no value provided (pipe it on stdin)", file=sys.stderr)
        return 2
    store.set(ns.name, value)
    print(f"{ns.name}: stored ({store.status(ns.name)['fingerprint']})")
    return 0


def cmd_show_config(ns: argparse.Namespace) -> int:
    _require_root()
    config = _load_config()
    data = config.to_public_dict()
    data["on_calendar"] = config.on_calendar()
    data["secrets"] = {name: config.secrets.status(name)["configured"] for name in KNOWN_SECRETS}
    try:
        data["borg_source"] = config.borg_source().describe()
    except ConfigurationError as exc:
        data["borg_source"] = f"unresolved: {exc}"
    _out(data, ns.json)
    return 0


# --------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_ID, description="Practical integrity testing of YunoHost Borg backups"
    )
    parser.add_argument("--version", action="version", version=f"{APP_ID} {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug output on stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run an integrity check now")
    p.add_argument("--mode", choices=["sampled", "full"], help="restore mode (default: configured)")
    p.add_argument(
        "--retain",
        nargs="?",
        const="",
        metavar="HOURS",
        help="keep the restore server for manual inspection (default: configured hours)",
    )
    p.add_argument(
        "--provider", help="override the configured cloud provider (e.g. static for tests)"
    )
    p.add_argument("--component", action="append", help="only these apps/components (repeatable)")
    p.add_argument(
        "--inspect-only",
        action="store_true",
        help="manifest, comparison and Borg checks only; no restore server",
    )
    p.add_argument(
        "--email", action="store_true", help="email the report according to the report policy"
    )
    p.add_argument(
        "--scheduled",
        action="store_true",
        help="unattended mode used by the systemd timer (implies --email)",
    )
    p.add_argument("--keep-host-on-failure", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true", help="print the report as JSON instead of text")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="show the latest run and schedule")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("history", help="list stored backup manifests and past runs")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("report", help="print the report of the latest (or given) run")
    p.add_argument("run_id", nargs="?")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("destroy", help="destroy the retained/leftover restore server of a run")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--all", action="store_true", help="every run with live resources")
    p.set_defaults(func=cmd_destroy)

    p = sub.add_parser("cleanup", help="find and destroy stale cloud resources created by this app")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument("--provider")
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser("test-config", help="test Borg access and cloud provider credentials")
    p.add_argument("what", nargs="?", choices=["borg", "provider", "all"], default="all")
    p.add_argument("--provider")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_test_config)

    p = sub.add_parser("catalog", help="list provider regions/sizes (used by the config panel)")
    p.add_argument("what", nargs="?", choices=["regions", "sizes", "all"], default="all")
    p.add_argument("--provider")
    p.add_argument("--region")
    p.add_argument(
        "--ynh-choices", action="store_true", help="print YunoHost config-panel 'choices:' YAML"
    )
    p.add_argument("--current", help="current value to keep selectable")
    p.add_argument("--extra", help="extra leading choice id (e.g. auto)")
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("secret", help="manage stored credentials (values are read from stdin)")
    p.add_argument("action", choices=["set", "status", "delete"])
    p.add_argument("name", nargs="?", choices=list(KNOWN_SECRETS))
    p.add_argument("--stdin", action="store_true")
    p.add_argument(
        "--allow-empty", action="store_true", help="treat an empty value as 'keep existing'"
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_secret)

    p = sub.add_parser("show-config", help="print the effective (non-secret) configuration")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if ns.command != "run":
        setup_logging(None, verbose=ns.verbose)
    try:
        return int(ns.func(ns))
    except IntegrityCheckError as exc:
        print(f"error: {redact(str(exc))}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

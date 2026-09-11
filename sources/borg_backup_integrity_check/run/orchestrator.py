"""IntegrityRun: the end-to-end integrity check (inspect -> provision -> restore -> sample -> verify -> report -> clean)."""

from __future__ import annotations

import contextlib
import random
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..borg.archives import ArchiveCatalog, ArchiveNamingScheme, BackupGeneration, component_kind
from ..borg.client import BorgClient, iter_cached_listing
from ..borg.layout import BackupLayout, read_layout
from ..borg.listing import DirectoryAggregates
from ..config import AppConfig, BorgSource
from ..discovery.components import Component, components_from_layout
from ..discovery.large_data import LargeDataDiscovery, is_db_dump_item
from ..discovery.profiles import ProfileRegistry
from ..errors import (
    BorgError,
    CleanupError,
    IntegrityCheckError,
    NoUsableBackupError,
    ProviderError,
    RestoreHostError,
)
from ..logging_setup import StageProgress, get_logger
from ..manifest.builder import ManifestBuilder
from ..manifest.compare import Comparison, ManifestComparator, Thresholds
from ..manifest.history import HistoryStore
from ..manifest.models import BackupManifest
from ..providers.base import CloudProvider, VMSpec, run_labels
from ..providers.registry import create_provider
from ..report.mail import send_report
from ..report.models import (
    FAIL,
    PASS,
    SKIPPED,
    CheckResult,
    ComponentReport,
    RetainedHost,
    RunReport,
    VerificationLevel,
)
from ..report.text import render_report
from ..restore.agent import HostAgent
from ..restore.bootstrap import RestoreHostBootstrap
from ..restore.cloud_init import build_user_data
from ..restore.engine import CoreRestoreEngine
from ..restore.health import ApplicationHealthChecker
from ..restore.payload import PayloadRetriever
from ..restore.planner import RestorePlan, build_plan
from ..restore.ssh import SSHSession
from ..sampling.sampler import GenericSampler, RootSample, SamplingRules
from ..yunohost import yunohost_version
from .cleanup import LifecycleManager
from .ids import host_name_for, new_run_id
from .state import RunState, RunStateStore

log = get_logger("run")
STAGES = 8


@dataclass
class RunOptions:
    mode: str = "sampled"
    retain_hours: float | None = None  # keep the host after the run
    provider_override: str | None = None
    components: list[str] | None = None
    inspect_only: bool = False
    email: bool = False
    quiet: bool = False
    keep_host_on_failure: bool | None = None
    run_id: str | None = None


class Interrupted(Exception):
    pass


class IntegrityRun:
    def __init__(self, config: AppConfig, options: RunOptions) -> None:
        self.config = config
        self.options = options
        self.run_id = options.run_id or new_run_id()
        self.paths = config.paths
        self.state_store = RunStateStore(self.paths.runs_dir)
        self.history = HistoryStore(self.paths.history_dir)
        self.progress = StageProgress(STAGES, quiet=options.quiet)
        self.report = RunReport(run_id=self.run_id, mode=options.mode, started_at=datetime.now())
        self.state: RunState | None = None
        self.provider: CloudProvider | None = None
        self.borg: BorgSource | None = None
        self.client: BorgClient | None = None
        self.generation: BackupGeneration | None = None
        self.layouts: dict[str, BackupLayout] = {}
        self.aggregates: dict[str, DirectoryAggregates] = {}
        self.listings: dict[str, Path] = {}
        self.components: list[Component] = []
        self.samples: dict[str, list[RootSample]] = {}
        self.plan: RestorePlan | None = None
        self.agent: HostAgent | None = None
        self.profiles = ProfileRegistry(
            [
                self.paths.install_dir / "profiles",
                Path(__file__).resolve().parent.parent / "profiles",
            ]
        )
        self._interrupted = False

    # ------------------------------------------------------------- entry
    def run(self) -> RunReport:
        provider_name = self.options.provider_override or self.config.provider_name
        self.report.provider = provider_name
        self.state = RunState(
            run_id=self.run_id,
            mode=self.options.mode,
            provider=provider_name,
            started_at=self.report.started_at,
            owner=self.config.owner_id,
        )
        self.state_store.save(self.state)
        self._install_signal_handlers()
        try:
            self._stage_inspect()
            if self.options.inspect_only:
                self.report.infos.append("Inspection-only run: no restore server was created.")
                self.report.cleanup_status = "not needed (inspection only)"
                self.state.cleanup_status = "not_needed"
            else:
                self._stage_provision(provider_name)
                self._stage_bootstrap()
                self._stage_restore_core()
                self._stage_samples()
                self._stage_verify()
        except Interrupted:
            self.report.fatal_error = "run interrupted (Ctrl-C / termination signal)"
            self.state.status = "interrupted"
        except NoUsableBackupError as exc:
            self.report.fatal_error = str(exc)
        except (BorgError, ProviderError, RestoreHostError, IntegrityCheckError) as exc:
            self.report.fatal_error = f"{exc.__class__.__name__}: {exc}"
            log.exception("run failed")
        except Exception as exc:  # noqa: BLE001 - never lose cleanup because of an unexpected error
            self.report.fatal_error = f"unexpected error: {exc.__class__.__name__}: {exc}"
            log.exception("unexpected failure")
        finally:
            self._stage_report()
            self._stage_cleanup()
            self._finish()
        return self.report

    # ------------------------------------------------------------ stage 1
    def _stage_inspect(self) -> None:
        self.progress.stage("Inspecting Borg backup")
        self.state.phase = "inspecting"
        self.state_store.save(self.state)
        self.borg = self.config.borg_source()
        self.client = BorgClient(
            binary=self.borg.binary,
            repository=self.borg.repository,
            passphrase=self.borg.passphrase,
            ssh_key_path=self.borg.ssh_key_path,
            remote_path=self.borg.remote_path,
            lock_wait=int(self.config.borg_lock_wait),
        )
        scheme = ArchiveNamingScheme.from_settings(
            str(self.config.archive_scheme), str(self.config.get("archive_pattern") or "") or None
        )
        archives = self.client.list_archives()
        if not archives:
            raise NoUsableBackupError("the Borg repository contains no archives")
        catalog = ArchiveCatalog.build(scheme, archives)
        window = timedelta(hours=int(self.config.generation_window_hours))
        generation = catalog.latest_generation(
            window, expected_components=self.history.known_components()
        )
        if generation is None:
            raise NoUsableBackupError("no archive matches the configured naming scheme")
        self.generation = generation
        self.report.backup_time = generation.timestamp
        self.progress.note(
            f"newest backup generation {generation.timestamp:%Y-%m-%d %H:%M} with {len(generation.archives)} archive(s)"
        )
        for comp, ref in generation.stale.items():
            self.report.warnings.append(
                f"Component '{comp}' has no archive in the newest backup generation (last seen {ref.start:%Y-%m-%d %H:%M})."
            )
        self._check_interrupt()

        workdir = self.paths.cache_dir / self.run_id
        workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        discovery = LargeDataDiscovery(self.profiles)
        for comp_name, ref in sorted(generation.archives.items()):
            self._check_interrupt()
            self.progress.note(f"reading archive {ref.name}")
            layout = read_layout(self.client, ref.name, workdir)
            self.layouts[ref.name] = layout
            listing = self.client.cache_listing(ref.name, workdir / f"{ref.name}.jsonl.gz")
            self.listings[ref.name] = listing
            agg = DirectoryAggregates.build(iter_cached_listing(listing), track=is_db_dump_item)
            self.aggregates[ref.name] = agg
            for component in components_from_layout(ref, comp_name, layout):
                discovery.discover(component, agg)
                self.components.append(component)
            if agg.unhealthy:
                self.report.warnings.append(
                    f"{ref.name}: {len(agg.unhealthy)} item(s) flagged unhealthy by Borg (missing chunks)."
                )
        self._select_samples()
        self._build_manifest_and_compare()
        self._borg_level_checks()
        self.state.phase = "inspected"
        self.state_store.save(self.state)

    def _select_samples(self) -> None:
        rules = SamplingRules(sample_size=int(self.config.sample_size))
        for component in self.components:
            if not component.large_roots:
                continue
            profile = self.profiles.find(component.app.manifest_id) if component.app else None
            comp_rules = (
                rules.with_profile_exclusions(
                    profile.exclude_dirs, profile.exclude_files, profile.include_globs
                )
                if profile
                else rules
            )
            sampler = GenericSampler(comp_rules)
            self.samples[component.id] = sampler.select(
                component, iter_cached_listing(self.listings[component.archive.name])
            )

    def _build_manifest_and_compare(self) -> None:
        builder = ManifestBuilder(self.client, repository_id=None)
        stats = builder.fetch_stats(self.generation)
        version = next(
            (
                lay.info.from_yunohost_version
                for lay in self.layouts.values()
                if lay.info.from_yunohost_version
            ),
            None,
        )
        manifest = builder.build(
            self.generation, self.components, stats=stats, yunohost_version=version
        )
        self.report.manifest = manifest
        if not self.config.manifest_compare:
            self.report.infos.append("Manifest comparison disabled in settings.")
            self.history.save(manifest)
            return
        previous = self.history.previous_to(manifest)
        if previous is None:
            # First run: build a cheap stats-only baseline from the previous generation in the repository.
            scheme = ArchiveNamingScheme.from_settings(
                str(self.config.archive_scheme),
                str(self.config.get("archive_pattern") or "") or None,
            )
            catalog = ArchiveCatalog.build(scheme, self.client.list_archives())
            prev_gen = catalog.previous_generation(
                self.generation, timedelta(hours=int(self.config.generation_window_hours))
            )
            if prev_gen is not None:
                previous = builder.build_stats_only(prev_gen)
        thresholds = Thresholds(
            growth_pct=float(self.config.warn_growth_pct),
            shrink_pct=float(self.config.warn_shrink_pct),
            count_growth_pct=float(self.config.warn_growth_pct),
            count_shrink_pct=float(self.config.warn_shrink_pct),
            max_backup_age_hours=float(self.config.backup_max_age_hours),
        )
        history = (
            self.history.load_all()
            if str(self.config.baseline_mode) == "previous_and_rolling"
            else []
        )
        comparison: Comparison = ManifestComparator(thresholds).compare(
            manifest, previous, history=history
        )
        self.report.comparison = comparison
        self.history.save(manifest)
        self.history.prune(int(self.config.history_retention_days))
        if comparison.anomalies:
            self.progress.warn(f"{len(comparison.anomalies)} manifest anomaly(ies) detected")

    def _borg_level_checks(self) -> None:
        level = str(self.config.borg_check_level)
        if level in ("archives", "repository"):
            self.progress.note("running borg check on the selected archives")
            failures = []
            for ref in self.generation.archives.values():
                try:
                    self.client.check(archives_glob=ref.name, archives_only=True, timeout=6 * 3600)
                except BorgError as exc:
                    failures.append(f"{ref.name}: {exc}")
            if level == "repository":
                try:
                    self.client.check(repository_only=True, timeout=12 * 3600)
                except BorgError as exc:
                    failures.append(f"repository: {exc}")
            if failures:
                self.report.borg_check = CheckResult("Borg check", FAIL, "; ".join(failures)[:400])
                self.report.problems.append(
                    "Borg reported archive/repository consistency errors: " + failures[0][:200]
                )
            else:
                self.report.borg_check = CheckResult(
                    "Borg check",
                    PASS,
                    f"{len(self.generation.archives)} archive(s) metadata consistent"
                    + (" + repository" if level == "repository" else ""),
                    VerificationLevel.ARCHIVE_METADATA_FOUND,
                )
        else:
            self.report.borg_check = CheckResult("Borg check", SKIPPED, "disabled")
        deep = str(self.config.deep_check)
        if deep in ("sampled_dry_run", "full_dry_run"):
            self._deep_dry_run(deep)
        else:
            self.report.deep_check = CheckResult("Dry-run extraction", SKIPPED, "disabled")

    def _deep_dry_run(self, mode: str) -> None:
        """Read (decrypt/decompress/verify) payload chunks without writing them anywhere."""
        limit = int(self.config.deep_check_sample)
        checked = 0
        failures = []
        for component in self.components:
            if not component.large_roots:
                continue
            if mode == "full_dry_run":
                paths = [r.archive_path for r in component.large_roots]
            else:
                candidates = [
                    it.path
                    for it in iter_cached_listing(self.listings[component.archive.name])
                    if it.is_file and component.is_large(it.path) and it.size > 0
                ]
                if not candidates:
                    continue
                random.shuffle(candidates)
                paths = candidates[: max(1, limit // max(len(self.components), 1))]
            try:
                self.progress.note(
                    f"dry-run extracting {len(paths)} object(s) of {component.label}"
                )
                self.client.extract_from_list(
                    component.archive.name,
                    self.paths.cache_dir / self.run_id / "dryrun",
                    paths,
                    dry_run=True,
                    timeout=12 * 3600,
                )
                checked += len(paths)
            except BorgError as exc:
                failures.append(f"{component.label}: {exc}")
        if failures:
            self.report.deep_check = CheckResult(
                "Dry-run extraction", FAIL, "; ".join(failures)[:400]
            )
            self.report.problems.append("Dry-run extraction failed: " + failures[0][:200])
        else:
            self.report.deep_check = CheckResult(
                "Dry-run extraction",
                PASS,
                f"{checked} additional object(s) read and verified without materialising them"
                if mode == "sampled_dry_run"
                else "all large data read and verified",
                VerificationLevel.OBJECT_EXTRACTED,
            )

    # ------------------------------------------------------------ stage 2
    def _stage_provision(self, provider_name: str) -> None:
        self.progress.stage("Creating restore server")
        self.state.phase = "provisioning"
        self.state_store.save(self.state)
        self.plan = build_plan(
            self.components,
            self.samples,
            self.options.mode,
            self.options.components or self.config.components_list,
            self.config.never_restore,
            min_memory_mb=int(self.config.vm_min_memory_mb),
        )
        for comp_id, reason in self.plan.skipped:
            self.report.infos.append(f"{comp_id}: {reason}")
        self.progress.note(self.plan.summary())
        self.provider = create_provider(
            provider_name, self.config.credentials(), self.config.provider_settings
        )
        major = self._yunohost_major()
        region = self._region_for(provider_name)
        self.state.region = region
        need_disk = self.plan.required_disk_gb()
        preferred = str(
            self.config.hetzner_server_type
            if provider_name == "hetzner"
            else self.config.digitalocean_size
        )
        volume_policy = str(self.config.restore_volume)
        volume_gb = 0
        try:
            if not self.provider.capabilities.sizes:
                # Pre-existing host (testing): nothing to size, trust whatever it has.
                size = self.provider.choose_size(region, 0, 0)
            else:
                size = self.provider.choose_size(
                    region,
                    self.plan.min_memory_mb,
                    need_disk if volume_policy == "never" else 0,
                    preferred=preferred,
                )
                if volume_policy == "always" or (
                    volume_policy == "auto" and size.disk_gb < need_disk
                ):
                    if not self.provider.capabilities.volumes:
                        raise LookupError(
                            f"needs {need_disk} GB but {size.id} only has {size.disk_gb} GB and volumes are unsupported"
                        )
                    volume_gb = max(need_disk - 4, 10)
        except LookupError as exc:
            raise ProviderError(f"cannot size the restore server: {exc}") from exc
        image = self.provider.find_image("debian", major, size.architecture)
        public_key = self._restore_host_public_key()
        labels = run_labels(self.run_id, self.config.owner_id)
        ssh_key = self.provider.ensure_ssh_key(f"bbic-{self.config.owner_id}", public_key, labels)
        self.state.ssh_key_id = ssh_key.id
        ssh_port = self.provider.maintenance_ssh_port(int(self.config.vm_ssh_port))
        spec = VMSpec(
            name=host_name_for(self.run_id),
            region=region,
            size=size.id,
            image=image,
            ssh_keys=[ssh_key],
            user_data=build_user_data(
                host_name_for(self.run_id), public_key, ssh_port, self.run_id
            ),
            labels=labels,
            enable_ipv6=True,
        )
        self.progress.note(
            f"provider {self.provider.display_name}: {size.id} ({size.memory_mb} MB RAM, {size.disk_gb} GB disk) in {region}, image {image.name}"
            + (f", +{volume_gb} GB volume" if volume_gb else "")
        )
        vm = self.provider.create_vm(spec)
        self.state.server_id, self.state.server_name = vm.id, vm.name
        self.state_store.save(self.state)
        vm = self.provider.wait_for_vm(vm.id, timeout=900)
        address = vm.ipv6 if self.config.prefer_ipv6 and vm.ipv6 else vm.address
        if not address:
            raise ProviderError("the restore server has no public address")
        self.state.host_address, self.state.host_ssh_port = address, ssh_port
        self.state_store.save(self.state)
        if volume_gb:
            volume = self.provider.create_volume(
                f"bbic-{self.run_id}", volume_gb, region, labels, attach_to=vm.id
            )
            self.state.volume_ids.append(volume.id)
            self.state.notes.append(f"volume device {volume.device}")
            self.state_store.save(self.state)
        self.progress.note(
            f"server {vm.name} is up at {address}; waiting for SSH on port {ssh_port}"
        )
        ssh = SSHSession(
            address,
            ssh_port,
            self.paths.restore_host_key,
            self.paths.cache_dir / self.run_id / "known_hosts",
        )
        ssh.wait_ready(timeout=900)
        self.agent = HostAgent(ssh)
        self.agent.deploy()

    def _yunohost_major(self) -> str:
        policy = str(self.config.yunohost_version_policy)
        if policy in ("12", "13"):
            return policy
        for layout in self.layouts.values():
            if layout.info.yunohost_major:
                return str(layout.info.yunohost_major)
        return "12"

    def _region_for(self, provider_name: str) -> str:
        if provider_name == "hetzner":
            return str(self.config.hetzner_location)
        if provider_name == "digitalocean":
            return str(self.config.digitalocean_region)
        return "local"

    def _restore_host_public_key(self) -> str:
        pub = Path(str(self.paths.restore_host_key) + ".pub")
        if not pub.is_file():
            raise IntegrityCheckError(
                f"restore host SSH key {pub} is missing; reinstall or upgrade the app"
            )
        return pub.read_text(encoding="utf-8").strip()

    # ------------------------------------------------------------ stage 3
    def _stage_bootstrap(self) -> None:
        self.progress.stage("Installing YunoHost")
        self.state.phase = "bootstrapping"
        self.state_store.save(self.state)
        try:
            borg_version = self.client.version()
        except BorgError:
            borg_version = None
        bootstrap = RestoreHostBootstrap(
            self.agent,
            self.borg,
            self.progress,
            ssh_port=self.provider.maintenance_ssh_port(int(self.config.vm_ssh_port)),
            borg_version=borg_version,
        )
        device = None
        for note in self.state.notes:
            if note.startswith("volume device ") and note.split(" ", 2)[2] != "None":
                device = note.split(" ", 2)[2]
        result = bootstrap.run(
            self._yunohost_major(), int(self.config.borg_lock_wait), volume_device=device
        )
        self.report.infos.extend(result.notes)

    # ------------------------------------------------------------ stage 4
    def _stage_restore_core(self) -> None:
        self.progress.stage("Restoring core data")
        self.state.phase = "restoring"
        self.state_store.save(self.state)
        assert self.plan is not None
        engine = CoreRestoreEngine(self.agent, self.aggregates, int(self.config.vm_ssh_port))
        domains = self._domains()
        main_domain = self._main_domain()
        if self.plan.system_conf:
            outcomes = engine.restore_system(self.plan)
            for cp in self.plan.system_conf:
                report = ComponentReport(
                    id=cp.component.id,
                    label=cp.component.label,
                    kind="system_conf",
                    data_kind="config",
                )
                engine.apply_outcome(report, outcomes[cp.component.id], "Restore")
                report.finalize()
                self.report.components.append(report)
        else:
            for note in engine.ensure_domains(domains, main_domain):
                self.report.warnings.append(note)
        actions = engine.quarantine(sorted(set(domains + ([main_domain] if main_domain else []))))
        self.report.infos.append("restore host isolation: " + ", ".join(actions))
        for cp in self.plan.apps:
            self._check_interrupt()
            comp = cp.component
            self.progress.note(f"restoring {comp.id}")
            report = ComponentReport(
                id=comp.id,
                label=comp.id,
                kind="app",
                sample_target=int(self.config.sample_size) if comp.large_roots else 0,
                large_roots=[r.archive_path for r in comp.large_roots],
            )
            report.notes.extend(comp.notes)
            outcome = engine.restore_app(cp)
            engine.apply_outcome(report, outcome)
            self.report.components.append(report)
        for cp in self.plan.system_data:
            comp = cp.component
            report = ComponentReport(
                id=comp.id,
                label=comp.label,
                kind="system_data",
                sample_target=int(self.config.sample_size),
                large_roots=[r.archive_path for r in comp.large_roots],
            )
            self.report.components.append(report)

    def _domains(self) -> list[str]:
        domains = set()
        for comp in self.components:
            if comp.app and comp.app.domain:
                domains.add(comp.app.domain)
        return sorted(domains)

    def _main_domain(self) -> str | None:
        for layout in self.layouts.values():
            if layout.main_domain:
                return layout.main_domain
        return None

    # ------------------------------------------------------------ stage 5
    def _stage_samples(self) -> None:
        self.progress.stage("Retrieving recent samples")
        self.state.phase = "sampling"
        self.state_store.save(self.state)
        retriever = PayloadRetriever(
            self.agent, include_sender=bool(self.config.email_include_sender)
        )
        reports = {r.id: r for r in self.report.components}
        for cp in self.plan.apps + self.plan.system_data:
            self._check_interrupt()
            report = reports[cp.component.id]
            core_failed = any(c.status == FAIL for c in report.checks)
            if cp.payload_mode == "none":
                continue
            if core_failed and cp.component.is_app:
                report.notes.append("samples skipped because the core restore failed")
                continue
            objects = sum(len(s.objects) for s in cp.samples)
            report.candidates = sum(s.candidates_seen for s in cp.samples)
            self.progress.note(
                f"{cp.component.label}: retrieving {objects} object(s)"
                + (" and the full data set" if cp.payload_mode == "full" else "")
            )
            outcome = retriever.retrieve(cp)
            report.samples.extend(outcome.samples)
            for error in outcome.errors:
                report.add_check("Payload retrieval", FAIL, error[:300])
            if cp.payload_mode == "full":
                status = PASS if outcome.full_roots_failed == 0 else FAIL
                report.add_check(
                    "Full data restore",
                    status,
                    f"{outcome.full_roots_ok} data root(s) restored completely",
                    VerificationLevel.OBJECT_EXTRACTED,
                )
            report.data_kind = _data_kind(report)

    # ------------------------------------------------------------ stage 6
    def _stage_verify(self) -> None:
        self.progress.stage("Verifying applications")
        self.state.phase = "verifying"
        self.state_store.save(self.state)
        checker = ApplicationHealthChecker(self.agent)
        reports = {r.id: r for r in self.report.components}
        for cp in self.plan.apps:
            self._check_interrupt()
            report = reports[cp.component.id]
            if any(c.status == FAIL and c.name == "Core/configuration" for c in report.checks):
                report.finalize()
                continue
            profile = self.profiles.find(cp.component.app.manifest_id) if cp.component.app else None
            try:
                checker.check_app(cp.component, report, profile)
                if report.data_kind == "mail":
                    checker.verify_mail(report)
            except RestoreHostError as exc:
                report.add_check("Health checks", FAIL, str(exc)[:300])
            report.finalize()
        for cp in self.plan.system_data:
            report = reports[cp.component.id]
            if report.data_kind == "mail" or cp.component.id == "data_mail":
                try:
                    checker.verify_mail(report)
                except RestoreHostError as exc:
                    report.add_check("Mail server access", FAIL, str(exc)[:300])
            report.finalize()
        for report in self.report.components:
            if report.status == SKIPPED and report.checks:
                report.finalize()

    # ------------------------------------------------------------ stage 7
    def _stage_report(self) -> None:
        self.progress.stage("Generating report", index=7)
        self.state.phase = "reporting"
        self.report.finished_at = datetime.now()
        for report in self.report.components:
            if report.status == SKIPPED and (report.checks or report.samples):
                report.finalize()
        retain = self._retention_hours()
        if retain and self.state and self.state.server_id and self.report.fatal_error is None:
            self.state.retained_until = datetime.now() + timedelta(hours=retain)
            self.report.retained_host = RetainedHost(
                provider=self.report.provider or "",
                address=self.state.host_address or "",
                ssh_port=self.state.host_ssh_port or 22,
                ssh_user="root",
                expires_at=self.state.retained_until,
                domains=self._domains(),
                urls=[
                    f"https://{c.app.domain}{c.app.path or '/'}"
                    for c in self.components
                    if c.app and c.app.domain
                ],
            )
        run_dir = self.state_store.run_dir(self.run_id)
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = render_report(self.report)
        (run_dir / "report.txt").write_text(text, encoding="utf-8")
        import json

        (run_dir / "report.json").write_text(
            json.dumps(self.report.to_dict(), indent=1, default=str), encoding="utf-8"
        )
        self.state.report_path = str(run_dir / "report.txt")
        self.state.overall = self.report.overall
        self.state_store.save(self.state)

    def _retention_hours(self) -> float | None:
        if self.options.retain_hours:
            return self.options.retain_hours
        keep_on_failure = (
            self.options.keep_host_on_failure
            if self.options.keep_host_on_failure is not None
            else bool(self.config.keep_host_on_failure)
        )
        if (
            keep_on_failure
            and self.report.overall == "FAIL"
            and self.state
            and self.state.server_id
            and not self.options.inspect_only
        ):
            return float(self.config.retain_hours)
        return None

    # ------------------------------------------------------------ stage 8
    def _stage_cleanup(self) -> None:
        self.progress.stage("Cleaning up", index=8)
        self.state.phase = "cleaning"
        cache = self.paths.cache_dir / self.run_id
        import shutil

        shutil.rmtree(cache, ignore_errors=True)
        if not self.state.has_cloud_resources:
            if self.state.cleanup_status != "not_needed":
                self.state.cleanup_status = "not_needed"
            self.report.cleanup_status = (
                self.report.cleanup_status
                if self.options.inspect_only
                else "no cloud resources were created"
            )
            return
        if self.state.retained_until:
            self.state.status = "retained"
            self.report.cleanup_status = (
                f"restore server retained until {self.state.retained_until:%Y-%m-%d %H:%M}"
            )
            self.state_store.save(self.state)
            return
        manager = LifecycleManager(self.provider, self.state_store, self.config.owner_id)
        try:
            result = manager.destroy_run(self.state, reason="run finished")
            leftovers = manager.verify_gone(self.state)
            if leftovers:
                self.report.cleanup_error = "resources still present after deletion: " + ", ".join(
                    leftovers
                )
            else:
                self.report.cleanup_status = (
                    "all temporary resources destroyed (" + ", ".join(result.destroyed) + ")"
                )
        except (CleanupError, ProviderError) as exc:
            self.report.cleanup_error = str(exc)
            self.state.cleanup_status = "failed"
            self.state.cleanup_error = str(exc)
            self.state_store.save(self.state)

    def _finish(self) -> None:
        # Re-render so the final report carries the cleanup outcome.
        run_dir = self.state_store.run_dir(self.run_id)
        (run_dir / "report.txt").write_text(render_report(self.report), encoding="utf-8")
        if self.state.status not in ("retained", "interrupted"):
            self.state.status = "failed" if self.report.overall == "FAIL" else "finished"
        self.state.finished_at = datetime.now()
        self.state.overall = self.report.overall
        self.state_store.save(self.state)
        self.state_store.prune(int(self.config.keep_runs))
        if self.options.email:
            policy = str(self.config.report_on)
            if policy == "always" or self.report.overall != "PASS":
                subject = (
                    f"[{self.config.app_id}] {self.report.overall_line} - backup {self.report.backup_time:%Y-%m-%d %H:%M}"
                    if self.report.backup_time
                    else f"[{self.config.app_id}] {self.report.overall_line}"
                )
                try:
                    send_report(self.config.report_recipient(), subject, render_report(self.report))
                except IntegrityCheckError as exc:
                    log.error("could not email the report: %s", exc)
                    self.report.warnings.append(f"report email failed: {exc}")

    # ------------------------------------------------------------ signals
    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):  # noqa: ARG001
            self._interrupted = True
            log.warning("received signal %s, finishing current step then cleaning up", signum)

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError):  # not in the main thread
                signal.signal(sig, handler)

    def _check_interrupt(self) -> None:
        if self._interrupted:
            raise Interrupted()


def _data_kind(report: ComponentReport) -> str:
    kinds = [s.evidence.kind for s in report.samples if s.ok]
    if not kinds:
        return (
            "generic"
            if report.kind == "app"
            else ("config" if report.kind == "system_conf" else "generic")
        )
    counts = {k: kinds.count(k) for k in set(kinds)}
    top = max(counts, key=counts.get)
    if top == "email":
        return "mail"
    if top in ("image", "video", "audio"):
        return "media"
    if top == "git_repo":
        return "repo"
    return "file"


def run_subprocess_safe(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def yunohost_version_or_none() -> str | None:
    return yunohost_version()


__all__ = ["IntegrityRun", "RunOptions", "BackupManifest", "component_kind"]

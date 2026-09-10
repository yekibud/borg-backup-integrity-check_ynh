# Development notes

Living document for contributors and for future Fable/Claude sessions. No secrets here, ever.

## Architecture (packaging v2 app wrapping a Python application)

```
manifest.toml / config_panel.toml / scripts/*      native YunoHost packaging (install form, config panel, actions)
conf/                                              systemd service + timers, CLI wrapper template
sources/borg_backup_integrity_check/               the Python application (copied to /var/www/<app>/, run as root)
  config.py        AppConfig: reads /etc/yunohost/apps/<app>/settings.yml (the ONLY settings store) + SecretStore
  secrets.py       root-only /etc/<app>/secrets.json (tokens, passphrase) - never rendered back to the panel
  yunohost.py      borg_ynh discovery, installed apps, known_hosts lookups
  borg/            BorgClient (subprocess, redacted, modern exit codes), JSON models, naming scheme/generations,
                   listing aggregation, YunoHost backup layout reader (info.json, backup.csv, app settings)
  discovery/       Component model, LargeDataDiscovery (data_dir setting > manifest data_dir > system data parts >
                   profile > size heuristic), declarative SamplingProfile registry
  sampling/        GenericSampler: newest N objects per large root from listing metadata only
  evidence/        content sniffing + generic extractors (email, image/EXIF, video mvhd, git, documents)
  manifest/        manifest model/builder, HistoryStore, ManifestComparator (previous + 7/30-day medians)
  providers/       CloudProvider contract, JsonApi helper, Hetzner, DigitalOcean, StaticHost (tests)
  restore/         SSHSession, cloud-init user-data (maintenance sshd), HostAgent, host_helper (runs ON the VM),
                   bootstrap, planner, CoreRestoreEngine, PayloadRetriever, ApplicationHealthChecker
  run/             IntegrityRun orchestrator (8 stages), RunState store, LifecycleManager (cleanup/stale)
  report/          RunReport model + verification levels, plain-text renderer, sendmail delivery
  profiles/        optional TOML adapters (nextcloud, immich) - never required
tests/unit         64 tests, no infrastructure (fakes for Borg, provider and host agent)
dev/               VirtualBox workflow, synthetic Borg repositories, YunoHost UX test runner
```

Key design decisions:

* **One configuration model.** Install questions and config panel options are the same setting ids; the Python side only reads `settings.yml`. Secrets go through `borg-backup-integrity-check secret set NAME --stdin` (stdin, never argv) into a 0600 root file. Config-panel password fields use `bind = "null"` + `set__` setters; blank (or the literal `None` the core sends for untouched password inputs) keeps the stored value.
* **Sparse core restore through YunoHost itself.** On the disposable host, `host_helper restore-core` extracts an archive minus its large roots (keeping skeleton dirs with their ownership), rewrites `info.json` sizes (YunoHost checks free disk against them), rebuilds a `.tar` in `/home/yunohost.backup/archives/` and runs `yunohost backup restore`. Payload (sampled or full) is then extracted directly into the live paths with ancestor directory items for ownership.
* **Restore-host hazards handled** (learned from upstream sources): after a system restore `regen_conf()` moves sshd to the production port with `PermitRootLogin no` for public addresses and rebuilds nftables from the restored `firewall.yml` -> a dedicated maintenance sshd (cloud-init, port 22022) + `yunohost firewall open` after every restore; restored DynDNS keys would re-point the production domain -> `/etc/hosts` block + key/cron removal; restored postfix relay could send as production -> error transport; restored `borg`/`borgserver`/self apps are never restored and borg timers are disabled on the host.
* **Generic first.** Nothing in the core references Nextcloud/Immich/Roundcube/Hetzner specifically; profiles and providers are plug-ins behind small contracts.
* **Never overstate.** Every sampled object carries a `VerificationLevel`; the report distinguishes EXTRACTED AND READABLE from REFERENCED BY APPLICATION (name found in the restored DB) and VERIFIED THROUGH APPLICATION (e.g. Dovecot search by Message-ID).

## Important upstream discoveries (Sept 2026)

* YunoHost stable 12.1.41.2 (Debian 12); 13.x (Debian 13) is beta: `https://install.yunohost.org/trixie` with `-d testing`.
* Packaging v2 / helpers 2.1; `password`-type install answers are NOT saved as settings, only injected into the install env. Config panels: `bind="null"`, `get__/set__/validate__/run__` functions, `type="button"` actions run via `yunohost app action run <app> <panel.section.button>`; option ids must be unique file-wide; `app`-type options accept `filter` in manifests but the config-panel JSON schema rejects `filter` (use a select with a getter instead).
* borg_ynh 1.4.5~ynh3: settings `repository`, `passphrase` (yes, in settings.yml), `remote_path`; key `/root/.ssh/id_<app>_ed25519`; binary `<install_dir>/venv/bin/borg`; archives `auto_conf-<now>`, `auto_data-<now>`, `auto_<app>-<now>` each containing a YunoHost backup work dir (`info.json`, `backup.csv`, `apps/<app>/{settings,backup}`, `conf/`, `data/`).
* YunoHost restore: `RestoreManager` refuses backups < 4.2; `_postinstall_if_needed` uses `conf/ynh/current_host`; `ynh_restore` of a `data_dir` path that is missing from the archive is skipped, others are mandatory; tar restore extracts only targeted members into `/home/yunohost.backup/tmp/<name>` then copies -> plan ~3x core size.
* Borg 1.4: `list --json-lines` fields (path, type, size, mtime, user, group, healthy), `extract --dry-run` reads/decrypts/verifies chunks without writing, patterns `+ pf:` exact / `- pp:` prefix / `- sh:**` catch-all, `BORG_EXIT_CODES=modern` (70-75 lock, 80-87 connection).
* Hetzner: labels (63 chars), `label_selector`, server statuses, `POST /servers` with `user_data` (32 KiB), volumes need `location` or `server`; DigitalOcean: flat tags (`bbic-run-<id>`), `DELETE /v2/droplets?tag_name=`, `project_id` on droplet creation, rate limits 5000/h & 250/min.

## Milestones

* [x] M1 - research, architecture, generic layers (borg/discovery/sampling/evidence/manifest/report), providers, orchestrator, CLI, YunoHost packaging files, 64 unit tests incl. a fake end-to-end pipeline.
* [~] M2 - local VirtualBox workflow (`dev/vm.sh`: cloud image + NoCloud seed + NAT/intnet, works), Level 3 synthetic Borg repository tests pass with the real Borg 1.4.5 binary (`tests/integration`, marker `borg`); Level 2 YunoHost UX tests (`dev/ynh-test.sh`) written, not yet executed on a VM.
* [ ] M3 - Level 4 local end-to-end (production VM + static-provider target VM).
* [ ] M4 - Level 5 real Hetzner / DigitalOcean runs (billable, needs credentials from the maintainer).
* [ ] M5 - Level 6 real Borg repository run.

## Known gaps / open questions

* **Upstream bug (YunoHost 12.1.41.2):** a `password`-type install question hidden by a `visible` condition makes `app_install` crash (`TypeError ... NoneType` in `Popen` env: the core re-injects every password option into the script env without checking for `None`). Workaround in this app: a single always-visible `provider_token` question (stored under the selected provider's name by the install script) and an always-visible optional `borg_passphrase`. Worth reporting upstream (`src/app.py`, "Reinject user-provider passwords").

* Not yet exercised against a real YunoHost: the config panel getters (`choices:` YAML for dynamic selects), the exact env value the core sends for untouched password fields, and `type = "time"` handling in install forms.
* `host_helper.cmd_install_borg_app` installs borg_ynh from the catalog (`yunohost app install borg`); the archive name/version of borg on the restore host is not pinned to the production one.
* Full mode volume handling mounts the volume at `/home` and bind-mounts `/var/mail`; `/var/www` and databases stay on the root disk.
* Hetzner IPv6: the API returns a /64; we use `<prefix>::1`.
* `dev/get-borg.sh` downloads the standalone Borg binary for local tests only; GPG verification needs the Borg release key, which could not be fetched from keyservers in this environment (signature unverified, binary never deployed anywhere).

## Running things

```bash
python3 -m venv .venv && .venv/bin/pip install pytest pyyaml requests pycdlib jsonschema ruff pre-commit
.venv/bin/python -m pytest tests/unit -q          # level 1
pre-commit run --all-files                         # lint (ruff, shellcheck, toml/yaml checks)
dev/vm.sh help                                     # VirtualBox workflow (levels 2-4)
```

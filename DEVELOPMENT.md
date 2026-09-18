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
* Packaging v2 / helpers 2.1; `password`-type install answers are NOT saved as settings, only injected into the install env. Config panels: `bind="null"`, `get__/set__/validate__/run__` functions, `type="button"` actions run via `yunohost app action run <app> <panel.section.button>`; option ids must be unique file-wide; `app`-type options accept `filter` in manifests but the config-panel JSON schema rejects `filter` (use a select with a getter instead); `type = "alert"` works for banners, but its documented `icon` property is rejected by the catalogue's `config_panel.v1` schema - validate with `jsonschema` against `YunoHost/apps/schemas/` before relying on a property.
* borg_ynh 1.4.5~ynh3: settings `repository`, `passphrase` (yes, in settings.yml), `remote_path`; key `/root/.ssh/id_<app>_ed25519`; binary `<install_dir>/venv/bin/borg`; archives `auto_conf-<now>`, `auto_data-<now>`, `auto_<app>-<now>` each containing a YunoHost backup work dir (`info.json`, `backup.csv`, `apps/<app>/{settings,backup}`, `conf/`, `data/`).
* YunoHost restore: `RestoreManager` refuses backups < 4.2; `_postinstall_if_needed` uses `conf/ynh/current_host`; `ynh_restore` of a `data_dir` path that is missing from the archive is skipped, others are mandatory; tar restore extracts only targeted members into `/home/yunohost.backup/tmp/<name>` then copies -> plan ~3x core size.
* Borg 1.4: `list --json-lines` fields (path, type, size, mtime, user, group, healthy), `extract --dry-run` reads/decrypts/verifies chunks without writing, patterns `+ pf:` exact / `- pp:` prefix / `- sh:**` catch-all, `BORG_EXIT_CODES=modern` (70-75 lock, 80-87 connection).
* Hetzner: labels (63 chars), `label_selector`, server statuses, `POST /servers` with `user_data` (32 KiB), volumes need `location` or `server`; DigitalOcean: flat tags (`bbic-run-<id>`), `DELETE /v2/droplets?tag_name=`, `project_id` on droplet creation, rate limits 5000/h & 250/min.

## Milestones

Verified end-to-end (Level 4) report excerpt::

    OVERALL: PASS WITH 1 WARNING
    MAIL-LIKE COMPONENT: Mail data  PASS
      Mail server access:  PASS  20/20 sampled messages found through Dovecot
      Sample verification: VERIFIED THROUGH APPLICATION
    FILE/MEDIA APPLICATION: filebrowser  PASS
      Service health / HTTP / SSO / Database restore: PASS
      real photo names + EXIF dimensions, ODT documents shown
    ATTENTION: Total objects increased 86.9% since the previous backup (206 -> 385)


* [x] M1 - research, architecture, generic layers (borg/discovery/sampling/evidence/manifest/report), providers, orchestrator, CLI, YunoHost packaging files, 64 unit tests incl. a fake end-to-end pipeline.
* [x] M2 - local VirtualBox workflow (`dev/vm.sh`: cloud image + NoCloud seed + NAT/intnet, works), Level 3 synthetic Borg repository tests pass with the real Borg 1.4.5 binary (`tests/integration`, marker `borg`); Level 2 YunoHost UX tests (`dev/ynh-test.sh`) pass on a real YunoHost 12.1.41.2 VM (68/69, the remaining one was a test-suite issue: data_dir survives `app remove` without `--purge`).
* [x] M3 - Level 4 local end-to-end fully working: `bbic-prod` (YunoHost 12.1.41.2 + borg_ynh with a local repo + filebrowser + synthetic photos/documents/mails, real `borg` backups) -> this app on `bbic-prod` with the `static` provider -> `bbic-target` (fresh Debian 12 cloud image + maintenance sshd). Verified end to end: manifest + comparison against stored history (second run flagged +86.9% objects correctly), borg check + dry-run extraction, YunoHost install on the target, system parts restore (LDAP/settings/certs) with postinstall from the archive, quarantine, sparse app restore through `yunohost backup restore`, sampled payload placed in the live data dir, evidence (EXIF dimensions, ODT, e-mail subjects/senders), service/HTTP/SSO checks, report e-mail path. All confirmed on a clean run: borg_ynh is installed on the target after the restore (timer disabled), and Dovecot mail verification reaches VERIFIED THROUGH APPLICATION (20/20 sampled messages found by Message-ID after a maildir `force-resync`). The only warning on the final run was the genuine manifest anomaly (backup grew +86.9% objects between runs), correctly flagged.
* [x] M4 - Level 5 (real Hetzner Cloud) + Level 6 (real Hetzner Storage Box repo) confirmed end to end: provisioned a real VM, installed YunoHost, sparse-restored system config + a 256 GB Nextcloud, retrieved 20 real objects verified against the restored Nextcloud DB (photos/PDFs/ODTs), destroyed the VM (no leftover billable resources). Result: PASS WITH 1 WARNING (Nextcloud 503 in sampled mode, expected). DigitalOcean live run not done (needs a DO token); its backend has mocked contract tests.
* [x] M5 - Level 6 real Borg repository exercised as part of the Hetzner run (real Storage Box, 15 components, dedicated append-only key).

## Level 5/6 real test (Hetzner Cloud + a real Hetzner Storage Box)

Exercised against a real Storage Box repo (`ssh://uNNNNN@...:23/~/backup`, 15 components x 5 generations,
including a 256 GB / 276k-file Nextcloud, Immich, Synapse, Forgejo, ...). Findings:

* **Dedicated append-only key is the right model** (the maintainer pushed back on copying the real key
  onto the disposable VM). The app generates its own key; authorize its public key once on the repo server
  with a forced command, e.g. `command="borg serve --append-only --restrict-to-path /home/backup",restrict <pubkey>`.
  Hetzner Storage Boxes honour this (there was already a `root@borgtest.tld` key using the same pattern).
  The disposable VM then only ever gets a read/append-only key; the admin's real key never leaves the server.
  Set `borg_ssh_key = dedicated`; the config panel shows the public key to authorize.
* **`borg_repository_remote` is a footgun if stale.** It overrides the URL the restore VM uses; a value left
  over from a local test (a LAN IP) made the cloud VM try to reach an unreachable address. Leave it empty
  unless the restore host genuinely needs a different URL than the production server.
* **Full-listing cost is real** and motivated the `listed`/`borg info` optimization: inspecting all 15
  components' full listings took ~25 min over the internet; skipping unselected components' listings cuts
  that to the few components actually sampled.
* Cleanup verified: a mid-run failure still destroyed the provisioned Hetzner VM (confirmed via the API,
  no leftover billable resources).

## Known gaps / open questions

* **A restore host that stops answering ssh ends the run.** Connect-phase failures (the command
  provably never ran) are retried within a 10-minute budget with capped backoff, and quarantine now
  stops the restored fail2ban - it watches the maintenance sshd and this check opens hundreds of
  connections from one address. A host that is gone for longer still aborts the whole run instead of
  failing only the component in flight.

* **FUSE-mounted large data was tried and dropped (Sept 2026).** `borg mount` + an overlay per large
  root works on its own (53 GB / 173k-file Immich archive: metadata walk 24 s, `chown -R` 1m44s with an
  828 MB upper layer thanks to `metacopy=on`), but it cannot replace the truncation rules: `ynh_restore`
  moves an existing live path aside and `mv` cannot remove a mount point, so the mount can only go on
  *after* the app's restore - at which point the restore script has already run without its data. In
  real runs it also broke nextcloud and degraded evidence (Dovecot 0/20 instead of 14/20, mail subjects
  and git commit info lost). Sampled extraction stays the only mechanism; do not resurrect the mount
  without fixing both.

* **Upstream bug (YunoHost 12.1.41.2):** a `password`-type install question hidden by a `visible` condition makes `app_install` crash (`TypeError ... NoneType` in `Popen` env: the core re-injects every password option into the script env without checking for `None`). Workaround in this app: a single always-visible `provider_token` question (stored under the selected provider's name by the install script) and an always-visible optional `borg_passphrase`. Worth reporting upstream (`src/app.py`, "Reinject user-provider passwords").

* **Upstream bug (web admin):** a `number`/`range` option without an explicit `max` (or `min`) is unusable in
  the web admin: the core sends the bound as `null`, `configPanels.ts` only skips the rule when it is `undefined`,
  so `maxValue(null)` rejects every value with "Value must be a number equal or lesser than ." (empty bound).
  Always declare `min` and `max` on number options - fixed here for `install.sample_size`; the config panel
  already declared bounds everywhere.

* Verified on a real YunoHost 12.1.41.2: install form, secret storage, config panel read/apply, dynamic select getters, actions, timer regeneration, upgrade, backup/restore, failed-install cleanup. Core limitation found: `yunohost app config set app panel.section.option --value` cannot evaluate a `visible` condition referencing another option (KeyError); dependent options must be submitted at section level with `--args`, as the webadmin does.
* Not yet exercised against a real YunoHost: the config panel getters (`choices:` YAML for dynamic selects), the exact env value the core sends for untouched password fields, and `type = "time"` handling in install forms.
* `host_helper.cmd_install_borg_app` installs borg_ynh from the catalog (`yunohost app install borg`); the archive name/version of borg on the restore host is not pinned to the production one.
* Full mode volume handling mounts the volume at `/home` and bind-mounts `/var/mail`; `/var/www` and databases stay on the root disk.
* Hetzner IPv6: the API returns a /64; we use `<prefix>::1`.
* `dev/get-borg.sh` downloads the standalone Borg binary for local tests only; GPG verification needs the Borg release key, which could not be fetched from keyservers in this environment (signature unverified, binary never deployed anywhere).

## Level 4 quick reference

```bash
dev/vm.sh create bbic-prod 12 10 && dev/vm.sh yunohost bbic-prod bbic-prod.test && dev/vm.sh snapshot bbic-prod clean-yunohost
dev/level4-setup.sh prod bbic-prod filebrowser      # borg_ynh (local repo /opt/borg-repo), app, data, backup
dev/level4-setup.sh app bbic-prod                   # install/upgrade this app (reuses borg_ynh; dedicated key authorised)
dev/vm.sh create bbic-target 12 20 && dev/level4-setup.sh target bbic-target bbic-prod && dev/vm.sh snapshot bbic-target prepared
dev/level4-setup.sh run bbic-prod sampled           # ~15 min; report on bbic-prod: borg-backup-integrity-check report
dev/level4-setup.sh reset-target bbic-target        # back to the prepared snapshot before the next run
```

Gotchas learned: `VBoxManage snapshot take --live` on a 4 GB VM can take >30 min (memory image never converges) - use offline snapshots (`dev/vm.sh snapshot` stops the VM first); `yunohost app upgrade -f DIR` is a no-op for an unchanged version unless `--force`; string patches applied with Python must be verified (ruff reformatting silently broke two of them).

## Running things

```bash
python3 -m venv .venv && .venv/bin/pip install pytest pyyaml requests pycdlib jsonschema ruff pre-commit
.venv/bin/python -m pytest tests/unit -q          # level 1
pre-commit run --all-files                         # lint (ruff, shellcheck, toml/yaml checks)
dev/vm.sh help                                     # VirtualBox workflow (levels 2-4)
```

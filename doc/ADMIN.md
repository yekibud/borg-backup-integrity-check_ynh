## How a check works

1. **Inspect**: the newest backup generation is located in the Borg repository (borg_ynh creates `auto_conf-…`, `auto_data-…` and `auto_<app>-…` archives). Only metadata and the archive listing are read.
2. **Manifest**: logical sizes, object counts, database-dump sizes and large-data roots are recorded per component and compared with the previous backup and 7/30-day medians. Anomalies are shown at the top of every report.
3. **Provision**: a disposable Debian server is created at the configured provider, labelled with the run id (`bbic-run`, `bbic-managed`). A dedicated maintenance sshd on port 22022 keeps it reachable even after the restored YunoHost configuration changes the normal SSH port.
4. **Bootstrap**: YunoHost (same major version as the backup) and the upstream Borg app are installed; the repository credentials are deployed for the duration of the check. Borg timers on the test server are disabled and the Borg/backup apps are never restored, so the test server can never write into your real repository.
5. **Restore core**: system configuration (LDAP users, domains, certificates) and every selected app are restored through `yunohost backup restore` from a *sparse* copy of the archive: everything except the large data roots (`data_dir` of apps, mail, homes, multimedia).
6. **Samples**: the newest N objects of each large root are extracted directly into place (sampled mode) or the whole root is extracted (full mode). Evidence (file names, EXIF dates, e-mail subjects, commit info...) is extracted generically from the objects themselves.
7. **Verify**: services, HTTP endpoints, databases; sampled objects are looked up in the application database and, for mail, through Dovecot.
8. **Report & clean up**: the plain-text report is stored under `/var/lib/borg-backup-integrity-check/runs/<run id>/`, e-mailed if configured, and every cloud resource is destroyed (verified through the provider API).

The restore server is isolated: DynDNS updates are blocked, outbound mail is disabled and the restored domains resolve to the server itself.

## Command line

```bash
borg-backup-integrity-check run                 # sampled check with the configured provider
borg-backup-integrity-check run --mode full     # conventional complete restore
borg-backup-integrity-check run --retain 6      # keep the server 6 hours for manual inspection
borg-backup-integrity-check run --inspect-only  # manifest comparison + Borg checks, no server
borg-backup-integrity-check status
borg-backup-integrity-check history
borg-backup-integrity-check report [RUN_ID]
borg-backup-integrity-check destroy [--all]     # destroy retained/leftover servers
borg-backup-integrity-check cleanup [--dry-run] # find and destroy stale labelled resources
borg-backup-integrity-check test-config
```

All commands use the same configuration as the web admin (app settings + the root-only secret file `/etc/borg-backup-integrity-check/secrets.json`). Credentials never appear in logs, reports or command arguments.

## Inspecting a retained server

After `run --retain`, the report lists the address and the maintenance SSH command. Map the restored domains to that address in your workstation's `/etc/hosts` to browse the restored applications with the restored users and certificates. The server is destroyed automatically when the retention period ends (hourly janitor timer) or with `destroy`.

## Verification levels

Reports never overstate success. Each sampled object carries the strongest level reached: `LISTED IN ARCHIVE` < `EXTRACTED` < `EXTRACTED AND READABLE` < `REFERENCED BY APPLICATION` (its name is found in the restored application database) < `VERIFIED THROUGH APPLICATION` (retrieved through the application or its protocol, e.g. Dovecot for mail).

## Extending

Optional sampling profiles (`profiles/*.toml` in the install directory) can refine large-data discovery or exclusions for specific upstream apps; unknown apps get generic behaviour based on their `data_dir`, backup metadata and a size heuristic.

## Logs

* `/var/log/borg-backup-integrity-check/<run id>.log` - detailed, secret-redacted run log
* `/var/log/borg-backup-integrity-check/borg-backup-integrity-check.log` - scheduled runs output

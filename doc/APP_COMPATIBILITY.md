# Application compatibility

What this app can and cannot verify, based on real checks against real backups. "Verified" means
the application was restored on a disposable server from a production Borg backup and answered its
own health checks; "not restorable" means its restore fails on a **clean machine** - with or without
this app, so the finding is about the application, not about the check.

Please report what you observe (see the repository issues) so this list grows.

## Verified

| Application | Notes |
| --- | --- |
| element | web app, no service of its own |
| forgejo | database + repositories sampled, service and HTTP verified |
| friendica | database + daemon verified |
| immich | needs the plumbing-file rule below (its restore chowns a file in its data directory) |
| my_webapp | |
| nextcloud | 256 GB / 277k files, sampled; returns 503 until its data is complete, which is expected |
| wallabag2 | |
| wordpress | |
| System parts | users & groups (LDAP), YunoHost settings, certificates, home directories, mail, multimedia |

## Not restorable on a clean machine

These fail the same way during a real disaster recovery. Nothing in this app can fix them; exclude
them from checks (see below) until their package is fixed upstream.

| Application | What happens | Where it belongs |
| --- | --- | --- |
| jitsi | restore runs `prosodyctl mod_roster_command`, and `mod_roster_command.lua` is not installed on a fresh system | [jitsi_ynh](https://github.com/YunoHost-Apps/jitsi_ynh/issues) |
| rspamd | its package comes from a third-party repository; on a fresh system the package is missing after the restore and the service will not start | [rspamd_ynh](https://github.com/YunoHost-Apps/rspamd_ynh/issues) |
| rspamdui | `rspamadm: command not found` - same root cause as rspamd | [rspamdui_ynh](https://github.com/YunoHost-Apps/rspamdui_ynh/issues) |
| synapse | apt cannot satisfy `libpq-dev` when another app (immich) has added the PostgreSQL upstream repository to the same machine | [synapse_ynh](https://github.com/YunoHost-Apps/synapse_ynh/issues) |

Those applications install fine from the catalogue; it is the *restore* path on a machine that does
not already have their dependencies that breaks. When reporting them, say explicitly that the
restore was onto a clean host - maintainers restoring in place will not reproduce it.

## Excluding an application from checks

Configuration panel -> **Advanced** -> *Apps never restored on the test server*: a comma separated
list of app ids. Excluded applications appear in the report under "not checked", so their absence
is never mistaken for success. Their archives are still inspected by the Borg-level checks and by
the manifest comparison - only the restore is skipped.

## What an application needs to support sampled checks

A sampled check restores everything except the bulk of large data directories (photo libraries,
mail, file storage), then places the newest objects back. An application package works with it when:

1. its restore script tolerates a data directory that exists but is **not complete**;
2. the files its restore script reads itself (helper scripts, configuration, small dumps) are kept
   out of the bulk - this app restores small files even inside a directory too big to restore whole,
   but only up to a budget;
3. it does not fail on files it created itself in its data directory (immich's restore chowns
   `backups/restore_immich_db_backup.sh` unconditionally, which is the failure mode to avoid);
4. it can start without its bulk data - returning 5xx until the data is there is fine and is
   reported as a warning, not a failure.

A package that needs the whole data set can still be checked with `--mode full`, which restores
everything and needs a restore server large enough to hold it.

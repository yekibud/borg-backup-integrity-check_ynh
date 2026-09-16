<!--
N.B.: This README was manually written for this app. Once the app is in the YunoHost catalog,
it will be generated automatically from the manifest and the doc/ folder.
-->

# Borg Backup Integrity for YunoHost

[![Integration level](https://apps.yunohost.org/badge/integration/borg-backup-integrity-check)](https://ci-apps.yunohost.org/ci/apps/borg-backup-integrity-check/)

*Automated practical integrity testing of the Borg backups created by YunoHost.*

## Overview

Borg Backup Integrity regularly proves that your Borg backups are usable: it locates the newest backup generation, compares its manifest with previous backups, creates a disposable cloud server (Hetzner Cloud or DigitalOcean), installs YunoHost there, restores all core state, retrieves the newest ~20 real objects of every large data set, verifies services and shows recognisable evidence (real file names, photo dates, e-mail subjects) in a plain-text report - then destroys the server.

**Shipped version:** 0.1.0~ynh1

## Documentation and resources

* Admin documentation: [doc/ADMIN.md](doc/ADMIN.md)
* Application compatibility (verified, known-broken, and what a package needs): [doc/APP_COMPATIBILITY.md](doc/APP_COMPATIBILITY.md)
* Development notes: [DEVELOPMENT.md](DEVELOPMENT.md)
* Upstream Borg: <https://www.borgbackup.org>

## Developer info

Please send your pull request to the [`main` branch](https://github.com/yekibud/borg-backup-integrity-check_ynh/tree/main).

To try the `main` branch:

```bash
sudo yunohost app install https://github.com/yekibud/borg-backup-integrity-check_ynh --debug
or
sudo yunohost app upgrade borg-backup-integrity-check -u https://github.com/yekibud/borg-backup-integrity-check_ynh --debug
```

**More info regarding app packaging:** <https://yunohost.org/packaging_apps>

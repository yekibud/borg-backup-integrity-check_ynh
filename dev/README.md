# Development tooling

* `dev/vm.sh` - VirtualBox workflow (Debian cloud image + cloud-init NoCloud seed, NAT port forwards, internal lab network `bbic-lab` 10.43.0.0/24). No sudo needed.
* `dev/ynh-test.sh` - Level 2 YunoHost packaging/UX test suite, run on a VM as root.
* `dev/get-borg.sh` - standalone Borg binary for local Level 3 tests (`dev/local/bin/borg`).
* `dev/synth/build_repo.py` - synthetic borg_ynh-like repositories with scenarios (normal, shrink, grow, missing, emptydb).
* `dev/local/` - git-ignored local state (ssh keys, images, VMs, binaries). Never commit it.

Typical loop:

```bash
dev/get-borg.sh && .venv/bin/python -m pytest tests -q -m borg      # level 3
dev/vm.sh images && dev/vm.sh create bbic-prod 12 10 && dev/vm.sh yunohost bbic-prod bbic-prod.test
dev/vm.sh snapshot bbic-prod clean-yunohost
dev/vm.sh push bbic-prod && dev/vm.sh ssh bbic-prod bash /root/bbic-src/dev/ynh-test.sh   # level 2
dev/vm.sh restore bbic-prod clean-yunohost                                                 # back to clean
```

Level 4 (local end-to-end) uses two VMs: `bbic-prod` (YunoHost + borg_ynh + synthetic data + this app) and
`bbic-target` (fresh Debian, reached through the `static` provider: `BBIC_STATIC_HOST=10.43.0.20:22 borg-backup-integrity-check run --provider static`).

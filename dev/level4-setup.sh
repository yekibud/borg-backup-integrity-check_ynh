#!/bin/bash
# Level 4 local end-to-end: prepare the "production" VM (YunoHost + borg_ynh + apps + data + backups) and the
# restore-target VM (fresh Debian + maintenance sshd), then run this app with the static provider.
#
#   dev/level4-setup.sh prod bbic-prod [APP...]    install borg_ynh (local repo), apps (default: filebrowser), data, run a backup
#   dev/level4-setup.sh target bbic-target         prepare the target VM (maintenance sshd on 22022, root key of the app)
#   dev/level4-setup.sh app bbic-prod              install/refresh this app on the prod VM (reusing borg_ynh)
#   dev/level4-setup.sh run bbic-prod [MODE]       run the integrity check from the prod VM against the target
#   dev/level4-setup.sh reset-target bbic-target   restore the 'prepared' snapshot of the target (fresh Debian + maintenance sshd)
set -Eeuo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
VM="$HERE/vm.sh"
APP=borg-backup-integrity-check
BORG_REPO_PATH=/opt/borg-repo
BORG_PASS="level4-synthetic-passphrase"
PROD_LAB_IP=10.43.0.10
TARGET_LAB_IP=10.43.0.20
MAINT_PORT=22022

log() { printf '\033[1;35m[l4]\033[0m %s\n' "$*" >&2; }

prod_setup() {
    local vm="$1"; shift
    local apps=("$@"); [ ${#apps[@]} -gt 0 ] || apps=(filebrowser)
    local domain; domain=$("$VM" ssh "$vm" 'cat /etc/yunohost/current_host')
    log "prod VM $vm, domain $domain, apps: ${apps[*]}"
    "$VM" ssh "$vm" 'mkdir -p /opt/borg-repo && chmod 700 /opt/borg-repo'
    if ! "$VM" ssh "$vm" 'test -d /etc/yunohost/apps/borg'; then
        log "installing borg_ynh with a local repository ($BORG_REPO_PATH)"
        "$VM" ssh "$vm" "yunohost app install borg --force --args 'repository=$BORG_REPO_PATH&passphrase=$BORG_PASS&conf=1&data=1&apps=all&on_calendar=2099-01-01 00:00:00&mailalert=never'" | tail -n 3
    fi
    for app in "${apps[@]}"; do
        if "$VM" ssh "$vm" "test -d /etc/yunohost/apps/$app"; then log "$app already installed"; continue; fi
        log "installing $app"
        "$VM" ssh "$vm" "yunohost app install $app --force --args 'domain=$domain&path=/$app&init_main_permission=visitors&admin=bbicadmin&password=bbic-dev-password&language=en'" | tail -n 3 || true
    done
    log "creating a second user and synthetic data (files, photos, mails)"
    "$VM" ssh "$vm" "yunohost user create alice --fullname 'Alice Example' --domain $domain --password 'alice-dev-password' >/dev/null 2>&1 || true"
    "$VM" ssh "$vm" 'bash -s' < "$HERE/synth/populate_prod.sh"
    log "running the borg_ynh backup now (systemctl start borg)"
    "$VM" ssh "$vm" 'systemctl start borg.service; sleep 2; tail -n 5 /var/log/borg/borg.log; /var/www/borg/wrapper/borg list --short' | tail -n 12
}

# The repository is a local path on prod, so borg_ynh has no SSH key: the target uses this app's
# dedicated key, authorised on prod with a forced, repository-restricted `borg serve`.
authorize_dedicated_key() {
    local vm="$1"
    log "authorising the app's dedicated key on $vm (forced borg serve, restricted to $BORG_REPO_PATH)"
    "$VM" ssh "$vm" "pub=\$(cat /etc/$APP/keys/borg_repository_ed25519.pub); mkdir -p /root/.ssh; chmod 700 /root/.ssh; grep -qF \"\$pub\" /root/.ssh/authorized_keys 2>/dev/null || echo \"command=\\\"/var/www/borg/venv/bin/borg serve --restrict-to-repository $BORG_REPO_PATH\\\",restrict \$pub\" >> /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys"
    "$VM" ssh "$vm" "yunohost app setting $APP borg_ssh_key -v dedicated >/dev/null"
}

target_setup() {
    local vm="$1" prod="${2:-bbic-prod}"
    log "preparing target VM $vm: maintenance sshd on $MAINT_PORT and the app's restore-host key"
    local pub; pub=$("$VM" ssh "$prod" "cat /etc/$APP/keys/restore_host_ed25519.pub")
    "$VM" ssh "$vm" "mkdir -p /etc/borg-backup-integrity-check && printf '%s\n' '$pub' > /etc/borg-backup-integrity-check/authorized_keys && chmod 600 /etc/borg-backup-integrity-check/authorized_keys"
    "$HERE/../.venv/bin/python" - "$MAINT_PORT" <<'PY' | "$VM" ssh "$vm" 'bash -s'
import sys
sys.path.insert(0, "sources")
from borg_backup_integrity_check.restore.cloud_init import MAINTENANCE_SSHD_CONFIG, MAINTENANCE_SSHD_UNIT
port = sys.argv[1]
print("cat > /etc/borg-backup-integrity-check/sshd_config <<'EOF'\n" + MAINTENANCE_SSHD_CONFIG.format(port=port) + "EOF")
print("cat > /etc/systemd/system/bbic-sshd.service <<'EOF'\n" + MAINTENANCE_SSHD_UNIT + "EOF")
print("chmod 600 /etc/borg-backup-integrity-check/sshd_config; systemctl daemon-reload; systemctl enable --now bbic-sshd.service; systemctl is-active bbic-sshd")
PY
}

app_setup() {
    local vm="$1"
    "$VM" push "$vm"
    if "$VM" ssh "$vm" "test -d /etc/yunohost/apps/$APP"; then
        log "upgrading $APP from local sources"
        "$VM" ssh "$vm" "yunohost app upgrade $APP -f /root/bbic-src" | tail -n 3
    else
        log "installing $APP (reusing borg_ynh, static provider will be used at run time)"
        "$VM" ssh "$vm" "yunohost app install /root/bbic-src --force --args 'cloud_provider=hetzner&provider_token=0000000000000000000000000000000000000000000000000000000000000000&hetzner_location=fsn1&hetzner_server_type=auto&use_borg_ynh=1&borg_app=borg&borg_repository_remote=ssh://root@$PROD_LAB_IP$BORG_REPO_PATH&restore_mode=sampled&sample_size=20&schedule_enabled=0&schedule_time=09:00&report_email='" | tail -n 5
    fi
    "$VM" ssh "$vm" "yunohost app setting $APP vm_ssh_port -v $MAINT_PORT >/dev/null"
    authorize_dedicated_key "$vm"
    "$VM" ssh "$vm" "$APP show-config | grep -E 'borg_source|vm_ssh_port|borg_ssh_key'"
}

run_check() {
    local vm="$1" mode="${2:-sampled}"
    log "running the integrity check on $vm against $TARGET_LAB_IP:$MAINT_PORT ($mode)"
    "$VM" ssh "$vm" "BBIC_STATIC_HOST=$TARGET_LAB_IP:$MAINT_PORT $APP --verbose run --provider static --mode $mode" 2>&1 | tee "$HERE/local/level4-run-$(date +%Y%m%d%H%M%S).log" | tail -n 120
}

reset_target() {
    local vm="${1:-bbic-target}"
    "$VM" restore "$vm" prepared
}

case "${1:-}" in
    reset-target) reset_target "${2:-bbic-target}" ;;
    prod) shift; prod_setup "$@" ;;
    target) target_setup "${2:-bbic-target}" "${3:-bbic-prod}" ;;
    app) app_setup "${2:-bbic-prod}" ;;
    run) run_check "${2:-bbic-prod}" "${3:-sampled}" ;;
    *) sed -n '2,9p' "$0" ;;
esac

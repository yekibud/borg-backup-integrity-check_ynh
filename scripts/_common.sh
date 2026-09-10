#!/bin/bash

#=================================================
# COMMON VARIABLES AND CUSTOM HELPERS
#=================================================

bbic_cli="/usr/local/bin/borg-backup-integrity-check"
bbic_etc_dir="/etc/$app"
bbic_log_dir="/var/log/$app"

# Settings that are only exposed in the config panel (not asked at install) and their defaults.
# Keep in sync with DEFAULTS in sources/borg_backup_integrity_check/config.py.
bbic_default_settings=(
    "borg_ssh_key=auto"
    "borg_remote_path="
    "archive_scheme=borg_ynh"
    "archive_pattern="
    "generation_window_hours=12"
    "backup_max_age_hours=36"
    "yunohost_version_policy=match_backup"
    "restore_volume=auto"
    "vm_min_memory_mb=4096"
    "vm_ssh_port=22022"
    "prefer_ipv6=0"
    "components=all"
    "borg_check_level=archives"
    "deep_check=sampled_dry_run"
    "deep_check_sample=200"
    "manifest_compare=1"
    "warn_growth_pct=40"
    "warn_shrink_pct=20"
    "baseline_mode=previous_and_rolling"
    "history_retention_days=400"
    "schedule_frequency=daily"
    "schedule_weekday=Mon"
    "report_on=always"
    "retain_hours=4"
    "keep_runs=30"
    "keep_host_on_failure=0"
    "restore_borg_app=1"
    "never_restore_apps=borg,borgserver,borg-backup-integrity-check"
    "borg_lock_wait=900"
    "email_include_sender=1"
    "log_level=info"
    "hetzner_location=fsn1"
    "hetzner_server_type=auto"
    "digitalocean_region=fra1"
    "digitalocean_size=auto"
    "digitalocean_project="
    "borg_repository="
    "borg_repository_remote="
    "borg_app="
)

# Initialise every config-panel-only setting that does not exist yet.
bbic_set_default_settings() {
    local entry key value
    for entry in "${bbic_default_settings[@]}"; do
        key="${entry%%=*}"
        value="${entry#*=}"
        ynh_app_setting_set_default --key="$key" --value="$value"
    done
}

# Store a secret from a bash variable without ever echoing it (xtrace is disabled meanwhile).
# usage: bbic_store_secret NAME "$value"
bbic_store_secret() {
    local name="$1"
    local value="$2"
    local xtrace_enable
    xtrace_enable=$(set +o | grep xtrace)
    set +o xtrace
    if [[ -n "$value" && "$value" != "None" ]]; then
        printf '%s' "$value" | "$bbic_cli" secret set "$name" --stdin >/dev/null
    fi
    eval "$xtrace_enable"
}

bbic_secret_configured() {
    "$bbic_cli" secret status "$1" --json 2>/dev/null | grep -q '"configured": true'
}

# Deploy the Python sources into the install dir (root-owned, readable by the app group).
bbic_deploy_sources() {
    ynh_safe_rm "$install_dir/borg_backup_integrity_check"
    cp -a ../sources/borg_backup_integrity_check "$install_dir/"
    find "$install_dir/borg_backup_integrity_check" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
    chown -R "root:$app" "$install_dir"
    chmod -R go-w "$install_dir"
    ynh_config_add --template="bin-wrapper" --destination="$bbic_cli"
    chmod 0755 "$bbic_cli"
}

bbic_setup_directories() {
    mkdir -p "$bbic_etc_dir/keys"
    chmod 0700 "$bbic_etc_dir" "$bbic_etc_dir/keys"
    chown -R root:root "$bbic_etc_dir"
    mkdir -p "$data_dir/history" "$data_dir/runs" "$data_dir/cache"
    chmod 0700 "$data_dir" "$data_dir/history" "$data_dir/runs" "$data_dir/cache"
    mkdir -p "$bbic_log_dir"
    chmod 0750 "$bbic_log_dir"
    chown "root:$app" "$bbic_log_dir"
}

# SSH key used to log into the disposable restore host, plus an optional dedicated repository key.
bbic_generate_keys() {
    if [ ! -f "$bbic_etc_dir/keys/restore_host_ed25519" ]; then
        ssh-keygen -q -t ed25519 -N "" -C "$app restore-host" -f "$bbic_etc_dir/keys/restore_host_ed25519"
    fi
    if [ ! -f "$bbic_etc_dir/keys/borg_repository_ed25519" ]; then
        ssh-keygen -q -t ed25519 -N "" -C "$app borg-repository" -f "$bbic_etc_dir/keys/borg_repository_ed25519"
    fi
    chmod 0600 "$bbic_etc_dir"/keys/*_ed25519
    chmod 0644 "$bbic_etc_dir"/keys/*.pub
}

# Compute the systemd OnCalendar expression from the schedule settings.
bbic_on_calendar() {
    local time="${schedule_time:-09:00}"
    local hh="${time%%:*}"
    local mm="${time#*:}"
    mm="${mm%%:*}"
    local stamp
    stamp=$(printf '%02d:%02d:00' "$((10#$hh))" "$((10#${mm:-0}))")
    case "${schedule_frequency:-daily}" in
        weekly) echo "${schedule_weekday:-Mon} *-*-* $stamp" ;;
        monthly) echo "*-*-01 $stamp" ;;
        *) echo "*-*-* $stamp" ;;
    esac
}

# (Re)write the timer units and enable/disable them according to schedule_enabled.
bbic_apply_schedule() {
    local on_calendar
    # shellcheck disable=SC2034  # used by ynh_config_add as __ON_CALENDAR__
    on_calendar=$(bbic_on_calendar)
    ynh_config_add --template="systemd.timer" --destination="/etc/systemd/system/$app.timer"
    ynh_config_add --template="janitor.timer" --destination="/etc/systemd/system/$app-janitor.timer"
    systemctl daemon-reload
    systemctl enable "$app-janitor.timer" --quiet
    systemctl start "$app-janitor.timer"
    if [ "${schedule_enabled:-1}" == "1" ] || [ "${schedule_enabled:-1}" == "true" ]; then
        systemctl enable "$app.timer" --quiet
        systemctl restart "$app.timer"
    else
        systemctl disable --now "$app.timer" --quiet 2>/dev/null || true
    fi
}

bbic_register_service() {
    yunohost service add "$app" --description="Borg backup integrity checks (timer driven)" \
        --log="$bbic_log_dir/$app.log" \
        --test_status="systemctl show $app.service -p ActiveState --value | grep -v failed"
    # The service is started by its timer (or manually), never at boot.
    systemctl disable "$app.service" --quiet 2>/dev/null || true
}

bbic_unregister_service() {
    if ynh_hide_warnings yunohost service status "$app" >/dev/null; then
        yunohost service remove "$app"
    fi
}

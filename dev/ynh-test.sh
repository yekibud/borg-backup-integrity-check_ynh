#!/bin/bash
# shellcheck disable=SC2034  # variables are used inside eval'ed check expressions
# Level 2: exercise the native YunoHost packaging on a disposable VM (install form, secrets, config panel,
# actions, timer, upgrade, remove, reinstall). Runs ON the VM as root: dev/vm.sh push NAME && dev/vm.sh ssh NAME bash /root/bbic-src/dev/ynh-test.sh
set -Eeuo pipefail

APP=borg-backup-integrity-check
SRC=/root/bbic-src
CLI=/usr/local/bin/$APP
PASS=0; FAIL=0
ok() { PASS=$((PASS + 1)); printf '\033[1;32mPASS\033[0m %s\n' "$*"; }
ko() { FAIL=$((FAIL + 1)); printf '\033[1;31mFAIL\033[0m %s\n' "$*"; }
check() { if eval "$2"; then ok "$1"; else ko "$1"; fi; }
setting() { yunohost app setting "$APP" "$1" 2>/dev/null || true; }

TOKEN="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
NEWTOKEN="fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
PP="test-passphrase-Xy7"

section() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

section "clean slate"
yunohost app remove "$APP" >/dev/null 2>&1 || true

section "1. install with the native form (CLI --args), hetzner, manual borg source"
yunohost app install "$SRC" --force --args "cloud_provider=hetzner&hetzner_token=$TOKEN&hetzner_location=fsn1&hetzner_server_type=auto&use_borg_ynh=0&borg_repository=ssh://sam@domain.tld:22/~/backup&borg_passphrase=$PP&restore_mode=sampled&sample_size=15&schedule_enabled=1&schedule_time=09:30&report_email=" >/tmp/bbic-install.log 2>&1 && ok "install succeeded" || { ko "install failed"; tail -n 40 /tmp/bbic-install.log; }

section "2. settings and secrets"
check "cloud_provider stored" '[ "$(setting cloud_provider)" = "hetzner" ]'
check "sample_size stored" '[ "$(setting sample_size)" = "15" ]'
check "config-panel-only defaults initialised" '[ "$(setting deep_check)" = "sampled_dry_run" ] && [ "$(setting warn_growth_pct)" = "40" ]'
check "token NOT in settings.yml" '! grep -q "$TOKEN" /etc/yunohost/apps/$APP/settings.yml'
check "passphrase NOT in settings.yml" '! grep -q "$PP" /etc/yunohost/apps/$APP/settings.yml'
check "secrets file is root-only 0600" '[ "$(stat -c %a:%U /etc/$APP/secrets.json)" = "600:root" ]'
check "token stored in secret store" '$CLI secret status hetzner_token | grep -q configured'
check "passphrase stored in secret store" '$CLI secret status borg_passphrase | grep -q "configured ("'
check "token not in install log" '! grep -rq "$TOKEN" /var/log/yunohost/categories/operation/*install*'
check "CLI wrapper installed" '[ -x $CLI ] && $CLI --version | grep -q borg'
check "show-config resolves manual borg source" '$CLI show-config --json | grep -q "source: manual"'

section "3. services and timers"
check "service registered in YunoHost" 'yunohost service status $APP >/dev/null'
check "main timer enabled" 'systemctl is-enabled $APP.timer >/dev/null'
check "timer calendar matches 09:30 daily" 'systemctl show $APP.timer -p TimersCalendar --value | grep -q "09:30:00"'
check "janitor timer enabled" 'systemctl is-enabled $APP-janitor.timer >/dev/null'
check "service not enabled at boot" '! systemctl is-enabled $APP.service >/dev/null 2>&1'

section "4. config panel: read"
GET=$(yunohost app config get "$APP" --full --output-as json)
check "config get works" '[ -n "$GET" ]'
check "panels present" 'echo "$GET" | python3 -c "import json,sys; d=json.load(sys.stdin); ids={p[\"id\"] for p in d[\"panels\"]}; assert {\"source\",\"provider\",\"sampling\",\"monitoring\",\"schedule\",\"operations\",\"advanced\"} <= ids"'
check "token value never rendered" '! echo "$GET" | grep -q "$TOKEN"'
check "passphrase value never rendered" '! echo "$GET" | grep -q "$PP"'
check "token status shows Configured" 'echo "$GET" | grep -q "Configured"'
check "hetzner fields visible, DO fields hidden (visible expr)" 'echo "$GET" | python3 -c "
import json,sys; d=json.load(sys.stdin)
opts={o[\"id\"]:o for p in d[\"panels\"] for s in p[\"sections\"] for o in s[\"options\"]}
assert opts[\"hetzner_location\"][\"visible\"]==\"cloud_provider == \x27hetzner\x27\"
assert opts[\"digitalocean_region\"][\"visible\"]==\"cloud_provider == \x27digitalocean\x27\""'

section "5. config panel: change ordinary settings"
yunohost app config set "$APP" sampling.main.sample_size --value 25 >/dev/null 2>&1 && ok "set sample_size" || ko "set sample_size"
check "sample_size changed in canonical settings" '[ "$(setting sample_size)" = "25" ]'
check "CLI sees the same value" '$CLI show-config --json | grep -q "\"sample_size\": 25"'
yunohost app config set "$APP" --args "schedule_frequency=weekly&schedule_weekday=Fri&schedule_time=07:15&schedule_enabled=1" >/dev/null 2>&1 && ok "set schedule" || ko "set schedule"
check "timer regenerated for weekly Fri 07:15" 'systemctl show $APP.timer -p TimersCalendar --value | grep -q "Fri.*07:15:00"'
yunohost app config set "$APP" schedule.main.schedule_enabled --value 0 >/dev/null 2>&1 && ok "disable schedule" || ko "disable schedule"
check "timer disabled" '! systemctl is-enabled $APP.timer >/dev/null 2>&1'
yunohost app config set "$APP" schedule.main.schedule_enabled --value 1 >/dev/null 2>&1 || true
check "timer re-enabled" 'systemctl is-enabled $APP.timer >/dev/null'

section "6. config panel: secrets replacement"
BEFORE=$($CLI secret status hetzner_token --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["hetzner_token"]["fingerprint"])')
yunohost app config set "$APP" provider.main.hetzner_token --value "" >/dev/null 2>&1 || true
AFTER=$($CLI secret status hetzner_token --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["hetzner_token"]["fingerprint"])')
check "blank replacement keeps the token" '[ "$BEFORE" = "$AFTER" ]'
yunohost app config set "$APP" provider.main.hetzner_token --value "$NEWTOKEN" >/dev/null 2>&1 && ok "replace token via panel" || ko "replace token via panel"
AFTER2=$($CLI secret status hetzner_token --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["hetzner_token"]["fingerprint"])')
check "token fingerprint changed" '[ "$BEFORE" != "$AFTER2" ]'
check "new token not in settings.yml nor logs" '! grep -q "$NEWTOKEN" /etc/yunohost/apps/$APP/settings.yml && ! grep -rq "$NEWTOKEN" /var/log/yunohost/categories/operation/ 2>/dev/null'

section "7. provider switch exposes DO fields"
yunohost app config set "$APP" provider.main.cloud_provider --value digitalocean >/dev/null 2>&1 && ok "switch provider" || ko "switch provider"
check "provider switched" '[ "$(setting cloud_provider)" = "digitalocean" ]'
yunohost app config set "$APP" provider.main.digitalocean_token --value "dop_v1_$(printf 'a%.0s' $(seq 1 64))" >/dev/null 2>&1 && ok "store DO token" || ko "store DO token"
check "DO token configured" '$CLI secret status digitalocean_token | grep -q configured'
yunohost app config set "$APP" provider.main.cloud_provider --value hetzner >/dev/null 2>&1 || true

section "8. validation"
if yunohost app config set "$APP" sampling.main.components --value "bad;value!" >/dev/null 2>&1; then ko "invalid components accepted"; else ok "invalid components rejected"; fi
if yunohost app config set "$APP" sampling.main.sample_size --value 9999 >/dev/null 2>&1; then ko "out-of-range sample_size accepted"; else ok "out-of-range sample_size rejected"; fi

section "9. actions"
if yunohost app action run "$APP" source.tests.test_borg >/tmp/bbic-action.log 2>&1; then ko "test_borg unexpectedly succeeded (dummy repo)"; else ok "test_borg action reports failure for dummy repository"; fi
check "action output redacts passphrase" '! grep -q "$PP" /tmp/bbic-action.log'
if yunohost app action run "$APP" provider.tests.test_provider >/tmp/bbic-action2.log 2>&1; then ko "test_provider unexpectedly succeeded (dummy token)"; else ok "test_provider action reports failure for dummy token"; fi
check "action output redacts token" '! grep -q "$NEWTOKEN" /tmp/bbic-action2.log'
yunohost app action run "$APP" operations.maintenance.show_status >/dev/null 2>&1 && ok "show_status action" || ko "show_status action"

section "10. CLI uses the canonical configuration"
check "status works" '$CLI status | grep -q "No integrity check has run yet"'
check "history works" '$CLI history | grep -q "Backup manifests"'
check "test-config borg fails cleanly on dummy repo" '! $CLI test-config borg >/dev/null 2>&1'
check "inspect-only run fails cleanly and leaves a report" '! $CLI run --inspect-only --quiet >/dev/null 2>&1; ls /var/lib/$APP/runs/*/report.txt >/dev/null'
check "run log redacts passphrase" '! grep -rq "$PP" /var/log/$APP/'

section "11. upgrade keeps state"
yunohost app upgrade "$APP" -f "$SRC" >/tmp/bbic-upgrade.log 2>&1 && ok "upgrade from local sources" || { ko "upgrade failed"; tail -n 30 /tmp/bbic-upgrade.log; }
check "secrets survive upgrade" '$CLI secret status hetzner_token | grep -q configured'
check "settings survive upgrade" '[ "$(setting sample_size)" = "25" ]'
check "timer survives upgrade" 'systemctl is-enabled $APP.timer >/dev/null'

section "12. backup / restore"
yunohost backup create -n bbic_test --apps "$APP" >/dev/null 2>&1 && ok "backup created" || ko "backup failed"
yunohost app remove "$APP" >/dev/null 2>&1 && ok "removed" || ko "remove failed"
check "wrapper removed" '[ ! -e $CLI ]'
check "secrets removed" '[ ! -e /etc/$APP/secrets.json ]'
check "timers removed" '! systemctl list-unit-files | grep -q "^$APP"'
yunohost backup restore bbic_test --apps "$APP" >/tmp/bbic-restore.log 2>&1 && ok "restored from backup" || { ko "restore failed"; tail -n 30 /tmp/bbic-restore.log; }
check "secrets restored" '$CLI secret status hetzner_token | grep -q configured'
check "timer restored" 'systemctl is-enabled $APP.timer >/dev/null'
yunohost backup delete bbic_test >/dev/null 2>&1 || true

section "13. failed install cleanup"
yunohost app remove "$APP" >/dev/null 2>&1 || true
if yunohost app install "$SRC" --force --args "cloud_provider=hetzner&hetzner_token=&hetzner_location=fsn1&hetzner_server_type=auto&use_borg_ynh=0&borg_repository=ssh://x@y/./r&borg_passphrase=$PP&restore_mode=sampled&sample_size=20&schedule_enabled=1&schedule_time=09:00&report_email=" >/dev/null 2>&1; then ko "install without token unexpectedly succeeded"; else ok "install without token refused"; fi
check "no leftovers after failed install" '[ ! -e $CLI ] && [ ! -d /etc/yunohost/apps/$APP ]'

section "14. reinstall (digitalocean, reuse borg_ynh if present)"
if [ -d /etc/yunohost/apps/borg ]; then USE=1; else USE=0; fi
yunohost app install "$SRC" --force --args "cloud_provider=digitalocean&digitalocean_token=dop_v1_$(printf 'b%.0s' $(seq 1 64))&digitalocean_region=fra1&digitalocean_size=auto&digitalocean_project=&use_borg_ynh=$USE&borg_repository=ssh://sam@domain.tld:22/~/backup&borg_passphrase=$PP&restore_mode=sampled&sample_size=20&schedule_enabled=0&schedule_time=09:00&report_email=admin@example.org" >/tmp/bbic-install2.log 2>&1 && ok "reinstall with DigitalOcean" || { ko "reinstall failed"; tail -n 30 /tmp/bbic-install2.log; }
check "schedule disabled at install honoured" '! systemctl is-enabled $APP.timer >/dev/null 2>&1'
check "report_email stored" '[ "$(setting report_email)" = "admin@example.org" ]'
if [ "$USE" = "1" ]; then check "borg_ynh source discovered" '$CLI show-config --json | grep -q "borg_ynh:borg"'; fi

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

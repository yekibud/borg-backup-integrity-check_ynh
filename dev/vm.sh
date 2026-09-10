#!/bin/bash
# VirtualBox development workflow for borg-backup-integrity-check_ynh (no sudo needed).
#
#   dev/vm.sh images                      download/verify Debian cloud images (12 and 13)
#   dev/vm.sh create NAME [12|13] [IDX]   create + boot a VM from a cloud image (IDX -> lab IP 10.43.0.IDX, ports 22IDX/80IDX/443IDX)
#   dev/vm.sh ssh NAME [CMD...]           ssh as root (or run a command)
#   dev/vm.sh yunohost NAME [DOMAIN]      install YunoHost (curl install.yunohost.org -a) and postinstall DOMAIN
#   dev/vm.sh push NAME                   copy this repository to /root/bbic-src on the VM
#   dev/vm.sh snapshot NAME SNAP | restore NAME SNAP | snapshots NAME
#   dev/vm.sh start|stop|destroy|status|info NAME
#   dev/vm.sh serial NAME                 tail the serial console log
#
# State lives in dev/local/ (git-ignored): ssh key, images, VM folders, per-VM info.
set -Eeuo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
LOCAL="$HERE/local"
IMAGES="$LOCAL/images"
VMS="$LOCAL/vms"
KEY="$LOCAL/ssh/id_ed25519"
INTNET="bbic-lab"
PY="$REPO/.venv/bin/python"
DEB12_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2"
DEB13_URL="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2"

log() { printf '\033[1;34m[vm]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[vm] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

ensure_key() {
    mkdir -p "$LOCAL/ssh"
    if [ ! -f "$KEY" ]; then
        ssh-keygen -q -t ed25519 -N "" -C "bbic-dev" -f "$KEY"
        log "generated dev ssh key $KEY"
    fi
}

verify_image() {
    local file="$1" base="$2"
    local sums
    sums="$IMAGES/SHA512SUMS-$(basename "$(dirname "$base")")"
    curl -fsSL "$(dirname "$base")/SHA512SUMS" -o "$sums"
    (cd "$IMAGES" && grep " $(basename "$file")\$" "$sums" | sha512sum -c --quiet -) || return 1
}

cmd_images() {
    mkdir -p "$IMAGES"
    for url in "$DEB12_URL" "$DEB13_URL"; do
        local name; name=$(basename "$url")
        local file="$IMAGES/$name"
        # Reuse an already downloaded copy elsewhere if it verifies (saves 450 MB downloads).
        if [ ! -f "$file" ] && [ -f "$HOME/ynh-test-vm/images/$name" ]; then
            cp --reflink=auto "$HOME/ynh-test-vm/images/$name" "$file"
        fi
        if [ -f "$file" ] && verify_image "$file" "$url"; then
            log "$name verified"
            continue
        fi
        log "downloading $name"
        curl -fL --progress-bar "$url" -o "$file"
        verify_image "$file" "$url" || die "checksum mismatch for $name"
        log "$name verified"
    done
}

vm_dir() { echo "$VMS/$1"; }
vm_info() { cat "$(vm_dir "$1")/info.env"; }
vm_exists() { VBoxManage showvminfo "$1" >/dev/null 2>&1; }
vm_running() { VBoxManage showvminfo "$1" --machinereadable 2>/dev/null | grep -q '^VMState="running"'; }

cmd_create() {
    local name="$1" major="${2:-12}" idx="${3:-10}"
    [ -n "$name" ] || die "usage: create NAME [12|13] [IDX]"
    vm_exists "$name" && die "VM $name already exists (destroy it first)"
    ensure_key
    [ -f "$IMAGES/debian-$major-generic-amd64.qcow2" ] || cmd_images
    local dir; dir=$(vm_dir "$name"); mkdir -p "$dir"
    local disk="$dir/disk.vdi" seed="$dir/seed.iso"
    local mac1 mac2 lab_ip ssh_port http_port https_port maint_port
    mac1=$(printf '080027AA%04X' "$((idx))"); mac2=$(printf '080027BB%04X' "$((idx))")
    lab_ip="10.43.0.$idx"; ssh_port="22$(printf '%02d' "$idx")"; http_port="80$(printf '%02d' "$idx")"; https_port="443$(printf '%02d' "$idx")"; maint_port="220$(printf '%02d' "$idx")"
    log "converting cloud image to VDI"
    VBoxManage clonemedium disk "$IMAGES/debian-$major-generic-amd64.qcow2" "$disk" --format VDI >/dev/null
    VBoxManage modifymedium disk "$disk" --resize 40960 >/dev/null
    log "building cloud-init seed"
    local pub; pub=$(cat "$KEY.pub")
    sed -e "s|__HOSTNAME__|$name|" -e "s|__PUBKEY__|$pub|" "$HERE/cloud-init/user-data.tpl" > "$dir/user-data"
    printf 'instance-id: %s-%s\nlocal-hostname: %s\n' "$name" "$(date +%s)" "$name" > "$dir/meta-data"
    sed -e "s|__MAC1__|$(echo "$mac1" | sed 's/../&:/g;s/:$//' | tr 'A-F' 'a-f')|" -e "s|__MAC2__|$(echo "$mac2" | sed 's/../&:/g;s/:$//' | tr 'A-F' 'a-f')|" -e "s|__LAB_IP__|$lab_ip|" "$HERE/cloud-init/network-config.tpl" > "$dir/network-config"
    "$PY" "$HERE/seed.py" "$seed" --user-data "$dir/user-data" --meta-data "$dir/meta-data" --network-config "$dir/network-config" >/dev/null
    log "registering VM $name (debian $major, lab ip $lab_ip, ssh 127.0.0.1:$ssh_port)"
    VBoxManage createvm --name "$name" --ostype Debian_64 --register --basefolder "$VMS" >/dev/null
    VBoxManage modifyvm "$name" --memory 4096 --cpus 2 --vram 16 --graphicscontroller vmsvga --audio-driver none \
        --boot1 disk --boot2 none --rtc-use-utc on --nic1 nat --macaddress1 "$mac1" --nic2 intnet --intnet2 "$INTNET" --macaddress2 "$mac2" \
        --nat-localhostreachable1 on \
        --natpf1 "ssh,tcp,127.0.0.1,$ssh_port,,22" --natpf1 "http,tcp,127.0.0.1,$http_port,,80" --natpf1 "https,tcp,127.0.0.1,$https_port,,443" --natpf1 "maint,tcp,127.0.0.1,$maint_port,,22022" \
        --uart1 0x3F8 4 --uartmode1 file "$dir/serial.log"
    VBoxManage storagectl "$name" --name SATA --add sata --controller IntelAhci --portcount 4
    VBoxManage storageattach "$name" --storagectl SATA --port 0 --device 0 --type hdd --medium "$disk"
    VBoxManage storageattach "$name" --storagectl SATA --port 1 --device 0 --type dvddrive --medium "$seed"
    cat > "$dir/info.env" <<EOF
NAME=$name
MAJOR=$major
IDX=$idx
LAB_IP=$lab_ip
SSH_PORT=$ssh_port
HTTP_PORT=$http_port
HTTPS_PORT=$https_port
MAINT_PORT=$maint_port
EOF
    cmd_start "$name"
    wait_ssh "$name"
    log "VM $name ready: dev/vm.sh ssh $name"
}

cmd_start() { vm_running "$1" || VBoxManage startvm "$1" --type headless >/dev/null; }
cmd_stop() { vm_running "$1" && { VBoxManage controlvm "$1" acpipowerbutton; for _ in $(seq 1 30); do vm_running "$1" || return 0; sleep 2; done; VBoxManage controlvm "$1" poweroff; } || true; }

cmd_destroy() {
    local name="$1"
    vm_exists "$name" || { log "VM $name does not exist"; rm -rf "$(vm_dir "$name")"; return 0; }
    vm_running "$name" && VBoxManage controlvm "$name" poweroff >/dev/null 2>&1 || true
    sleep 1
    VBoxManage unregistervm "$name" --delete >/dev/null
    rm -rf "$(vm_dir "$name")"
    log "destroyed $name"
}

ssh_opts() {
    local name="$1"; local port; port=$(vm_info "$name" | grep '^SSH_PORT=' | cut -d= -f2)
    echo "-p $port -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=$(vm_dir "$name")/known_hosts -o ConnectTimeout=10 -o LogLevel=ERROR"
}

cmd_ssh() {
    local name="$1"; shift
    # shellcheck disable=SC2046
    ssh $(ssh_opts "$name") root@127.0.0.1 "$@"
}

wait_ssh() {
    local name="$1"
    for _ in $(seq 1 90); do
        if cmd_ssh "$name" 'test -f /var/lib/bbic-dev-ready' 2>/dev/null; then return 0; fi
        sleep 4
    done
    die "VM $name did not become reachable over ssh (see dev/vm.sh serial $name)"
}

cmd_yunohost() {
    local name="$1" domain="${2:-$1.test}"
    local major; major=$(vm_info "$name" | grep '^MAJOR=' | cut -d= -f2)
    if cmd_ssh "$name" 'test -x /usr/bin/yunohost'; then log "YunoHost already installed on $name"; else
        log "installing YunoHost $major on $name (several minutes)..."
        if [ "$major" = "13" ]; then
            cmd_ssh "$name" 'curl -fsSL https://install.yunohost.org/trixie | bash -s -- -a -d testing' | tail -n 5
        else
            cmd_ssh "$name" 'curl -fsSL https://install.yunohost.org | bash -s -- -a' | tail -n 5
        fi
    fi
    if cmd_ssh "$name" 'test -f /etc/yunohost/installed'; then log "already post-installed"; else
        log "post-installing with domain $domain"
        cmd_ssh "$name" "yunohost tools postinstall --domain $domain --username bbicadmin --fullname 'BBIC Admin' --password 'bbic-dev-password' --ignore-dyndns --force-diskspace" | tail -n 3
    fi
    # Keep root reachable through the NAT port forward even after YunoHost regenerated sshd_config.
    cmd_ssh "$name" "grep -q bbic-dev /etc/ssh/sshd_config.d/99-bbic-dev.conf 2>/dev/null || true; yunohost settings set security.ssh.password_authentication -v false >/dev/null 2>&1 || true"
    log "YunoHost ready on $name (https://127.0.0.1:$(vm_info "$name" | grep '^HTTPS_PORT=' | cut -d= -f2)/yunohost/admin)"
}

cmd_push() {
    local name="$1"
    log "pushing repository to $name:/root/bbic-src"
    # shellcheck disable=SC2046
    tar -C "$REPO" --exclude=.git --exclude=.venv --exclude=dev/local --exclude='__pycache__' --exclude=.pytest_cache --exclude=.ruff_cache -czf - . | ssh $(ssh_opts "$name") root@127.0.0.1 'rm -rf /root/bbic-src && mkdir -p /root/bbic-src && tar -xzf - -C /root/bbic-src'
}

cmd_snapshot() { VBoxManage snapshot "$1" take "$2" --live >/dev/null && log "snapshot $2 taken on $1"; }
cmd_restore() {
    local name="$1" snap="$2"
    vm_running "$name" && VBoxManage controlvm "$name" poweroff >/dev/null 2>&1 || true
    sleep 1
    VBoxManage snapshot "$name" restore "$snap" >/dev/null
    cmd_start "$name"; wait_ssh "$name"
    log "restored $name to snapshot $snap"
}
cmd_snapshots() { VBoxManage snapshot "$1" list 2>/dev/null || echo "no snapshots"; }
cmd_status() { VBoxManage list runningvms; echo "--- all:"; VBoxManage list vms; }
cmd_info() { vm_info "$1"; }
cmd_serial() { tail -n 60 "$(vm_dir "$1")/serial.log"; }

case "${1:-help}" in
    images) cmd_images ;;
    create) cmd_create "${2:-}" "${3:-12}" "${4:-10}" ;;
    start) cmd_start "$2" ;;
    stop) cmd_stop "$2" ;;
    destroy) cmd_destroy "$2" ;;
    ssh) shift; cmd_ssh "$@" ;;
    yunohost) cmd_yunohost "$2" "${3:-}" ;;
    push) cmd_push "$2" ;;
    snapshot) cmd_snapshot "$2" "$3" ;;
    restore) cmd_restore "$2" "$3" ;;
    snapshots) cmd_snapshots "$2" ;;
    status) cmd_status ;;
    info) cmd_info "$2" ;;
    serial) cmd_serial "$2" ;;
    *) sed -n '2,15p' "$0" ;;
esac

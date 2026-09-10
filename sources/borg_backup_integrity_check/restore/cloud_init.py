"""cloud-init user-data for the disposable restore host.

Keeps the host reachable no matter what the restored YunoHost configuration
does to the standard sshd: a dedicated maintenance sshd instance listens on
``maintenance_port`` with its own config and authorized_keys file.
"""

from __future__ import annotations

import json

MAINTENANCE_SSHD_CONFIG = """# Maintenance sshd for borg-backup-integrity-check (independent of YunoHost's sshd)
Port {port}
ListenAddress 0.0.0.0
ListenAddress ::
PidFile /run/bbic-sshd.pid
HostKey /etc/ssh/ssh_host_ed25519_key
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthorizedKeysFile /etc/borg-backup-integrity-check/authorized_keys
AllowUsers root
UsePAM yes
ClientAliveInterval 30
ClientAliveCountMax 8
Subsystem sftp internal-sftp
"""

MAINTENANCE_SSHD_UNIT = """[Unit]
Description=Maintenance SSH server for borg-backup-integrity-check
After=network.target
ConditionPathExists=/etc/borg-backup-integrity-check/sshd_config

[Service]
ExecStartPre=/usr/sbin/sshd -t -f /etc/borg-backup-integrity-check/sshd_config
ExecStart=/usr/sbin/sshd -D -f /etc/borg-backup-integrity-check/sshd_config
ExecReload=/bin/kill -HUP $MAINPID
KillMode=process
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""


def build_user_data(hostname: str, public_key: str, maintenance_port: int, run_id: str) -> str:
    """Return a ``#cloud-config`` document (YAML emitted as JSON, which YAML accepts)."""
    doc = {
        "hostname": hostname,
        "manage_etc_hosts": True,
        "disable_root": False,
        "ssh_pwauth": False,
        "users": [
            {"name": "root", "ssh_authorized_keys": [public_key]},
        ],
        "write_files": [
            {
                "path": "/etc/borg-backup-integrity-check/sshd_config",
                "permissions": "0600",
                "content": MAINTENANCE_SSHD_CONFIG.format(port=maintenance_port),
            },
            {
                "path": "/etc/borg-backup-integrity-check/authorized_keys",
                "permissions": "0600",
                "content": public_key + "\n",
            },
            {
                "path": "/etc/systemd/system/bbic-sshd.service",
                "permissions": "0644",
                "content": MAINTENANCE_SSHD_UNIT,
            },
            {
                "path": "/etc/borg-backup-integrity-check/run-id",
                "permissions": "0644",
                "content": run_id + "\n",
            },
        ],
        "runcmd": [
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "--now", "bbic-sshd.service"],
        ],
    }
    return "#cloud-config\n" + json.dumps(doc, indent=1)

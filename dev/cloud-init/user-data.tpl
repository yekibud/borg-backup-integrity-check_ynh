#cloud-config
hostname: __HOSTNAME__
manage_etc_hosts: true
disable_root: false
ssh_pwauth: false
users:
  - name: root
    ssh_authorized_keys:
      - __PUBKEY__
growpart:
  mode: auto
  devices: ["/"]
resize_rootfs: true
package_update: false
write_files:
  - path: /etc/ssh/sshd_config.d/99-bbic-dev.conf
    permissions: "0644"
    content: |
      PermitRootLogin prohibit-password
      PasswordAuthentication no
runcmd:
  - [systemctl, restart, ssh]
  - [sh, -c, "echo bbic-dev-ready > /var/lib/bbic-dev-ready"]

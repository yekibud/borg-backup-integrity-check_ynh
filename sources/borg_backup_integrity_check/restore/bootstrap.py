"""RestoreHostBootstrap: from a freshly booted Debian VM to a YunoHost with Borg access."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import BorgSource
from ..errors import RestoreHostError
from ..logging_setup import get_logger
from ..yunohost import known_host_entry
from .agent import HostAgent

log = get_logger("bootstrap")


@dataclass
class BootstrapResult:
    yunohost_installed: bool
    borg_app_installed: bool
    borg_access_ok: bool
    notes: list[str]


class RestoreHostBootstrap:
    def __init__(self, agent: HostAgent, borg: BorgSource, progress=None) -> None:
        self.agent = agent
        self.borg = borg
        self.progress = progress

    def _note(self, text: str) -> None:
        log.info(text)
        if self.progress:
            self.progress.note(text)

    def wait_cloud_init(self) -> None:
        result = self.agent.call("wait-cloud-init", timeout=1200)
        if result.get("rc") not in (0, 2):  # 2 = done with recoverable errors
            log.warning("cloud-init status: %s", result.get("status"))

    def mount_volume(self, device: str) -> None:
        result = self.agent.call("mount-volume", args=["--device", device], timeout=3600)
        if not result.get("ok"):
            raise RestoreHostError(
                f"could not mount the temporary volume {device}: {result.get('error')}"
            )

    def install_yunohost(self, major: str) -> None:
        self._note(f"installing YunoHost {major} (this takes several minutes)")
        result = self.agent.call("install-yunohost", args=["--major", major], timeout=4200)
        if not result.get("ok"):
            raise RestoreHostError(
                f"YunoHost installation failed: {result.get('log_tail', result.get('error', ''))[-1200:]}"
            )

    def deploy_borg_credentials(self, lock_wait: int) -> None:
        if not self.borg.ssh_key_path or not Path(self.borg.ssh_key_path).is_file():
            key_text = ""
            self._note(
                "no SSH key for the repository (local repository or password-less access assumed)"
            )
        else:
            key_text = Path(self.borg.ssh_key_path).read_text(encoding="utf-8")
        host = _repository_host(self.borg.remote_repository)
        known = known_host_entry(host) if host else None
        spec = {
            "repository": self.borg.remote_repository,
            "passphrase": self.borg.passphrase or "",
            "private_key": key_text,
            "known_hosts": known or "",
            "remote_path": self.borg.remote_path or "",
            "lock_wait": lock_wait,
        }
        result = self.agent.call("deploy-credentials", spec=spec, timeout=120)
        if not result.get("ok"):
            raise RestoreHostError(f"could not deploy Borg credentials: {result.get('error')}")

    def install_borg_app(self) -> bool:
        self._note("installing upstream borg_ynh (Borg client) on the restore host")
        result = self.agent.call("install-borg-app", timeout=4200)
        if not result.get("ok"):
            log.warning(
                "borg_ynh installation failed: %s", result.get("log_tail", result.get("error"))
            )
            return False
        return True

    def verify_borg_access(self) -> None:
        result = self.agent.call("borg-test", timeout=900)
        if not result.get("ok"):
            raise RestoreHostError(
                f"the restore host cannot read the Borg repository: {result.get('error')}"
            )
        self._note(
            f"repository reachable from the restore host ({len(result.get('archives', []))} archive(s) listed)"
        )

    def run(self, major: str, lock_wait: int, volume_device: str | None = None) -> BootstrapResult:
        notes: list[str] = []
        self.wait_cloud_init()
        if volume_device:
            self.mount_volume(volume_device)
            notes.append(f"temporary volume mounted from {volume_device}")
        self.install_yunohost(major)
        self.deploy_borg_credentials(lock_wait)
        borg_app = self.install_borg_app()
        if not borg_app:
            notes.append(
                "borg_ynh could not be installed; falling back to the Debian borgbackup package"
            )
            self.agent.ssh.run(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y -q borgbackup", timeout=1800
            )
        self.verify_borg_access()
        return BootstrapResult(True, borg_app, True, notes)


def _repository_host(repository: str) -> str | None:
    if not repository.startswith("ssh://"):
        return None
    rest = repository[len("ssh://") :]
    hostpart = rest.split("/", 1)[0]
    if "@" in hostpart:
        hostpart = hostpart.split("@", 1)[1]
    if hostpart.startswith("["):
        return (
            hostpart.split("]")[0] + "]" + hostpart.split("]", 1)[1]
            if ":" in hostpart.split("]", 1)[1]
            else hostpart.split("]")[0] + "]"
        )
    if ":" in hostpart:
        host, port = hostpart.rsplit(":", 1)
        return f"[{host}]:{port}" if port != "22" else host
    return hostpart

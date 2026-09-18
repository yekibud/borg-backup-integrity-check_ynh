"""Transport-level behaviour of SSHSession: what is worth retrying and what is not."""

from __future__ import annotations

import subprocess

import pytest

from borg_backup_integrity_check.errors import RestoreHostError
from borg_backup_integrity_check.restore import ssh as ssh_mod

KEX_RESET = b"kex_exchange_identification: read: Connection reset by peer\r\n"
MID_SESSION_DROP = b"client_loop: send disconnect: Connection reset by peer\r\n"
HOST_KEY_CHANGED = (
    b"@@@ REMOTE HOST IDENTIFICATION HAS CHANGED @@@\nHost key verification failed.\n"
)


class _Proc:
    def __init__(self, rc: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


@pytest.fixture
def session(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    sleeps: list[float] = []
    responses: list[_Proc] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return responses.pop(0)

    monkeypatch.setattr(ssh_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(ssh_mod.time, "sleep", lambda seconds: sleeps.append(seconds))
    ssh = ssh_mod.SSHSession("203.0.113.7", 22022, tmp_path / "key", tmp_path / "known_hosts")
    return ssh, calls, sleeps, responses


def test_connection_reset_before_the_command_is_retried(session):
    ssh, calls, sleeps, responses = session
    responses += [_Proc(255, b"", KEX_RESET), _Proc(255, b"", KEX_RESET), _Proc(0, b"ok\n")]
    assert ssh.run("hostname").stdout == "ok\n"
    assert len(calls) == 3
    assert sleeps == [5, 15]  # the restore host is given time to finish restarting sshd


def test_retrying_is_bounded_by_a_time_budget_then_reported(session):
    """A restore can bounce sshd for minutes; what bounds the wait is time, not a retry count."""
    ssh, calls, sleeps, responses = session
    responses += [_Proc(255, b"", KEX_RESET) for _ in range(200)]
    ssh.connect_retry_budget = 600

    with pytest.raises(RestoreHostError, match="after waiting"):
        ssh.run("hostname")

    assert sum(sleeps) <= 600 and sum(sleeps) > 500, "the whole budget is used before giving up"
    assert len(calls) > 5, "a two-minute outage no longer loses the run"
    assert max(sleeps) == 30, "backoff is capped so the host is polled steadily"


def test_a_session_that_dies_mid_command_is_not_repeated(session):
    """The remote command may have run (a restore takes hours); repeating it is the caller's call."""
    ssh, calls, _, responses = session
    responses.append(_Proc(255, b"", MID_SESSION_DROP))
    with pytest.raises(RestoreHostError):
        ssh.run("restore-core")
    assert len(calls) == 1


def test_a_changed_host_key_fails_immediately(session):
    ssh, calls, _, responses = session
    responses.append(_Proc(255, b"", HOST_KEY_CHANGED))
    with pytest.raises(RestoreHostError, match="Host key verification failed"):
        ssh.run("hostname")
    assert len(calls) == 1


def test_a_timeout_is_never_retried(session, monkeypatch):
    ssh, calls, _, _ = session

    def timing_out(args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, 40)

    monkeypatch.setattr(ssh_mod.subprocess, "run", timing_out)
    with pytest.raises(RestoreHostError, match="timed out"):
        ssh.run("hostname", timeout=40)
    assert len(calls) == 1


def test_wait_ready_does_its_own_pacing(session):
    ssh, calls, sleeps, responses = session
    responses += [_Proc(255, b"", KEX_RESET), _Proc(0, b"bbic-ready\n")]
    ssh.wait_ready(timeout=60, interval=1)
    assert len(calls) == 2 and sleeps == [1]  # no nested back-off inside the readiness loop

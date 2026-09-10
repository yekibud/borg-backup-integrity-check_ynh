"""Exception hierarchy shared by every layer of the application."""

from __future__ import annotations


class IntegrityCheckError(Exception):
    """Base class for all errors raised by this application."""

    exit_code = 1


class ConfigurationError(IntegrityCheckError):
    """The canonical YunoHost configuration is missing or inconsistent."""

    exit_code = 2


class BorgError(IntegrityCheckError):
    """A Borg command failed. Messages are already redacted."""

    exit_code = 3

    def __init__(self, message: str, rc: int | None = None, category: str = "error") -> None:
        super().__init__(message)
        self.rc = rc
        self.category = category

    @property
    def retryable(self) -> bool:
        return self.category in {"lock", "connection"}


class ProviderError(IntegrityCheckError):
    """A cloud provider API call failed."""

    exit_code = 4

    def __init__(self, message: str, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class RestoreHostError(IntegrityCheckError):
    """A remote command on the disposable restore host failed."""

    exit_code = 5


class CleanupError(IntegrityCheckError):
    """Temporary cloud resources could not be destroyed. Must be reported prominently."""

    exit_code = 6


class NoUsableBackupError(IntegrityCheckError):
    """No sufficiently recent/complete backup generation was found."""

    exit_code = 7

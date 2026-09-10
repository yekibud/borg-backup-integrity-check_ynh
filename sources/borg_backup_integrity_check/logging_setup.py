"""Two-channel logging: concise stage progress for admins, detailed redacted debug log.

* stdout gets ``[n/N] Stage name`` lines and warnings (human friendly).
* a per-run log file receives DEBUG output from every module, with secrets redacted.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path

from .redaction import redact

LOGGER_NAME = "bbic"


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        return redact(super().format(record))


class StageProgress:
    """Prints ``[i/N] message`` lines; counts stages so numbering stays consistent."""

    def __init__(self, total: int, stream=None, quiet: bool = False) -> None:
        self.total = total
        self.index = 0
        self.stream = stream or sys.stdout
        self.quiet = quiet
        self.log = logging.getLogger(LOGGER_NAME)

    def stage(self, message: str, index: int | None = None) -> None:
        self.index = index if index is not None else self.index + 1
        line = f"[{self.index}/{self.total}] {message}"
        self.log.info("STAGE %s", line)
        if not self.quiet:
            print(line, file=self.stream, flush=True)

    def note(self, message: str) -> None:
        self.log.info(message)
        if not self.quiet:
            print(f"      {message}", file=self.stream, flush=True)

    def warn(self, message: str) -> None:
        self.log.warning(message)
        if not self.quiet:
            print(f"      WARNING: {message}", file=self.stream, flush=True)


def setup_logging(
    log_file: Path | None = None,
    verbose: bool = False,
    quiet: bool = False,
) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    if quiet and not verbose:
        console.setLevel(logging.ERROR)
    console.setFormatter(RedactingFormatter("%(levelname)s: %(message)s"))
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        with contextlib.suppress(OSError):
            os.chmod(log_file, 0o640)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    if name:
        return logging.getLogger(f"{LOGGER_NAME}.{name}")
    return logging.getLogger(LOGGER_NAME)

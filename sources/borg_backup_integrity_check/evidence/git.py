"""Git repository evidence read from the repository's own metadata files (no git binary needed)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from .models import Evidence

# Files worth retrieving from a repository to describe it (tiny, no payload).
GIT_METADATA_FILES = ("HEAD", "logs/HEAD", "packed-refs", "config", "description")
_REFLOG_RE = re.compile(
    r"^(?P<old>[0-9a-f]{40}) (?P<new>[0-9a-f]{40}) (?P<who>.*?) (?P<ts>\d{9,11}) (?P<tz>[+-]\d{4})\t(?P<msg>.*)$"
)


def extract_git_repo(repo_dir: Path, mtime: datetime | None, size: int | None = None) -> Evidence:
    git_dir = repo_dir / ".git" if (repo_dir / ".git").is_dir() else repo_dir
    details: dict = {}
    when, when_source = mtime, "mtime"
    error = None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        if head.startswith("ref: "):
            details["branch"] = head[5:].removeprefix("refs/heads/")
        else:
            details["head"] = head[:12]
        reflog = git_dir / "logs" / "HEAD"
        if reflog.is_file():
            last = ""
            with open(reflog, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.strip():
                        last = line.rstrip("\n")
            match = _REFLOG_RE.match(last)
            if match:
                details["last_commit"] = match.group("new")[:12]
                details["last_message"] = match.group("msg")[:120]
                ts = int(match.group("ts"))
                when, when_source = datetime.fromtimestamp(ts, tz=UTC), "commit"
        refs = git_dir / "refs" / "heads"
        if refs.is_dir():
            details["branches"] = sum(1 for _ in refs.rglob("*") if _.is_file())
    except OSError as exc:
        error = str(exc)
    return Evidence(
        kind="git_repo",
        title=repo_dir.name or str(repo_dir),
        when=when,
        when_source=when_source,
        mime="application/x-git",
        size=size,
        path=str(repo_dir),
        details=details,
        readable=error is None and ("branch" in details or "head" in details),
        error=error,
    )

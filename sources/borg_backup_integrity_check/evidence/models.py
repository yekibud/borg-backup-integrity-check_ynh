"""Evidence records produced for every sampled object."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Evidence:
    """Human-recognisable description of a sampled object (never its payload)."""

    kind: (
        str  # email | image | video | audio | document | archive | git_repo | text | file | unknown
    )
    title: str  # subject, filename or repo path
    when: datetime | None  # the most meaningful timestamp (Date header, EXIF, commit, mtime)
    when_source: str = "mtime"  # mtime | header_date | exif | commit
    mime: str | None = None
    size: int | None = None
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    readable: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "when": self.when.isoformat() if self.when else None,
            "when_source": self.when_source,
            "mime": self.mime,
            "size": self.size,
            "path": self.path,
            "details": dict(self.details),
            "readable": self.readable,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Evidence:
        when = data.get("when")
        return cls(
            kind=data.get("kind", "unknown"),
            title=data.get("title", ""),
            when=datetime.fromisoformat(when) if when else None,
            when_source=data.get("when_source", "mtime"),
            mime=data.get("mime"),
            size=data.get("size"),
            path=data.get("path"),
            details=dict(data.get("details", {}) or {}),
            readable=bool(data.get("readable", True)),
            error=data.get("error"),
        )

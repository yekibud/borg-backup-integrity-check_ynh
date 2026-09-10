"""RFC 5322 / MIME message evidence: subject, date, sender - never the body."""

from __future__ import annotations

from datetime import datetime
from email import policy
from email.parser import BytesHeaderParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from .models import Evidence

MAX_HEADER_BYTES = 256 * 1024


def extract_email(
    path: Path, size: int | None, mtime: datetime | None, include_sender: bool = True
) -> Evidence:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_HEADER_BYTES)
        if raw.startswith(b"From "):
            raw = raw.split(b"\n", 1)[1] if b"\n" in raw else b""
        msg = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:  # noqa: BLE001 - any parse failure is evidence of unreadability
        return Evidence(
            kind="email",
            title=path.name,
            when=mtime,
            size=size,
            readable=False,
            error=f"unparseable message: {exc}",
        )

    subject = _header(msg, "Subject") or "(no subject)"
    when, when_source = mtime, "mtime"
    date_header = _header(msg, "Date")
    if date_header:
        try:
            when = parsedate_to_datetime(date_header)
            when_source = "header_date"
        except (TypeError, ValueError):
            pass
    details: dict = {}
    if include_sender:
        sender = _header(msg, "From")
        if sender:
            name, addr = parseaddr(sender)
            details["from"] = name or addr
            details["from_address"] = addr
    message_id = _header(msg, "Message-ID")
    if message_id:
        details["message_id"] = message_id.strip()
    if msg.get_content_type():
        details["content_type"] = msg.get_content_type()
    readable = bool(subject != "(no subject)" or date_header or message_id or details.get("from"))
    return Evidence(
        kind="email",
        title=subject.strip()[:200],
        when=when,
        when_source=when_source,
        mime="message/rfc822",
        size=size,
        path=str(path),
        details=details,
        readable=readable,
        error=None if readable else "no recognisable email headers",
    )


def _header(msg, name: str) -> str | None:
    try:
        value = msg.get(name)
    except Exception:  # noqa: BLE001 - malformed header
        return None
    return str(value) if value is not None else None

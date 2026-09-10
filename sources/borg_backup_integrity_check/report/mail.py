"""Email delivery through the server's own MTA (``sendmail``), no external SMTP stack."""

from __future__ import annotations

import socket
import subprocess
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from ..errors import IntegrityCheckError
from ..logging_setup import get_logger

log = get_logger("mail")


def send_report(
    recipient: str,
    subject: str,
    body: str,
    sender: str | None = None,
    sendmail: str = "/usr/sbin/sendmail",
) -> None:
    hostname = socket.getfqdn() or socket.gethostname()
    msg = EmailMessage()
    msg["From"] = sender or f"Borg Backup Integrity <root@{hostname}>"
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=hostname)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body)
    try:
        proc = subprocess.run(
            [sendmail, "-t", "-oi"],
            input=msg.as_bytes(),
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrityCheckError(f"could not run {sendmail}: {exc}") from exc
    if proc.returncode != 0:
        raise IntegrityCheckError(
            f"sendmail failed (rc={proc.returncode}): {proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    log.info("report emailed to %s", recipient)

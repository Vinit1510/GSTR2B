"""Email utility to send GSTR-2B attachments to clients."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger("gstr2b.mailer")

def send_gstr2b_email(
    client_name: str,
    recipient_email: str,
    attachment_path: Path,
    month: int,
    year: int,
    settings: Optional[dict] = None
) -> bool:
    """Send an email with the GSTR-2B attachment. Returns True if successful."""
    if not settings:
        settings = config.load_settings()

    if not settings.get("smtp_user") or not settings.get("smtp_pass"):
        log.warning("Email settings not configured (missing user/pass). Skipping.")
        return False

    if not recipient_email or "@" not in recipient_email:
        log.warning("Invalid recipient email for %s: %s", client_name, recipient_email)
        return False

    if not attachment_path.exists():
        log.error("Attachment not found: %s", attachment_path)
        return False

    month_name = ["January", "February", "March", "April", "May", "June",
                  "July", "August", "September", "October", "November", "December"][month - 1]

    msg = EmailMessage()
    
    # Subject and body with placeholders
    subject = settings.get("email_subject", "").format(
        client_name=client_name, month=month_name, year=year
    )
    body = settings.get("email_body", "").format(
        client_name=client_name, month=month_name, year=year
    )

    msg["Subject"] = subject
    msg["From"] = f"{settings.get('sender_name')} <{settings.get('smtp_user')}>"
    msg["To"] = recipient_email
    msg.set_content(body)

    # Attach file
    try:
        with open(attachment_path, "rb") as f:
            file_data = f.read()
            file_name = attachment_path.name
        msg.add_attachment(
            file_data,
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=file_name
        )
    except Exception as exc:
        log.exception("Failed to read attachment %s: %s", attachment_path, exc)
        return False

    # Send
    try:
        host = settings.get("smtp_server", "smtp.gmail.com")
        port = int(settings.get("smtp_port", 465))
        
        # Use SSL for 465, STARTTLS for 587
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as server:
                server.login(settings["smtp_user"], settings["smtp_pass"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.starttls()
                server.login(settings["smtp_user"], settings["smtp_pass"])
                server.send_message(msg)
        
        log.info("Email sent successfully to %s", recipient_email)
        return True
    except Exception as exc:
        log.error("Failed to send email to %s: %s", recipient_email, exc)
        return False

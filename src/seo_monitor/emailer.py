"""Send a report by email using SMTP settings from .env."""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


class EmailNotConfigured(Exception):
    pass


def send_report(subject: str, summary_html: Path, report_html: Path) -> list[str]:
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    recipients = [r.strip() for r in os.getenv("EMAIL_TO", "").split(",") if r.strip()]
    missing = [
        name
        for name, val in [
            ("SMTP_HOST", host),
            ("SMTP_USER", user),
            ("SMTP_PASSWORD", password),
            ("EMAIL_TO", recipients),
        ]
        if not val
    ]
    if missing:
        raise EmailNotConfigured(f"set {', '.join(missing)} in .env (see .env.example)")
    port = int(os.getenv("SMTP_PORT", "587"))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_FROM", "").strip() or user
    msg["To"] = ", ".join(recipients)
    msg.set_content("This report is HTML. Open the attachment for full details.")
    msg.add_alternative(summary_html.read_text(encoding="utf-8"), subtype="html")
    msg.add_attachment(
        report_html.read_bytes(), maintype="text", subtype="html", filename=report_html.name
    )

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=context)
            smtp.login(user, password)
            smtp.send_message(msg)
    return recipients

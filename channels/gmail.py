"""
Gmail IMAP read + SMTP send.

Credentials via env (do not hardcode secrets):
  GMAIL_EMAIL
  GMAIL_APP_PASSWORD
  GMAIL_IMAP_SERVER  (optional, default imap.gmail.com)
  GMAIL_SMTP_SERVER  (optional, default smtp.gmail.com)
  GMAIL_SMTP_PORT    (optional, default 587)
"""

from __future__ import annotations

import email
import imaplib
import os
import smtplib
from email.header import decode_header
from email.message import EmailMessage
from typing import Any

from dotenv import load_dotenv

load_dotenv()

EMAIL = os.environ.get("GMAIL_EMAIL", "")
PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
IMAP_SERVER = os.environ.get("GMAIL_IMAP_SERVER", "imap.gmail.com")
SMTP_SERVER = os.environ.get("GMAIL_SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("GMAIL_SMTP_PORT", "587"))


def _decode_header_value(raw: str | None) -> str:
    if not raw:
        return ""
    parts: list[str] = []
    for value, encoding in decode_header(raw):
        if isinstance(value, bytes):
            parts.append(value.decode(encoding or "utf-8", errors="ignore"))
        else:
            parts.append(value)
    return " ".join(parts).strip()


def _body_snippet(msg: email.message.Message, limit: int = 200) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition") or "")
            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(errors="ignore")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(errors="ignore")
    return " ".join(body.strip().split())[:limit]


def _account() -> dict[str, Any]:
    from user_scope import current_user_id

    if current_user_id():
        from accounts import email_credentials

        creds = email_credentials()
        if not creds:
            raise RuntimeError(
                "Email is not configured for this account. Add it under Email in the console."
            )
        return creds
    if not EMAIL or not PASSWORD:
        raise RuntimeError(
            "Set GMAIL_EMAIL and GMAIL_APP_PASSWORD environment variables."
        )
    return {
        "email": EMAIL,
        "app_password": PASSWORD,
        "imap_server": IMAP_SERVER,
        "smtp_server": SMTP_SERVER,
        "smtp_port": SMTP_PORT,
    }


def read_last_emails(
    limit: int = 10,
    *,
    unread_only: bool = False,
    mark_as_read: bool = True,
) -> list[dict[str, Any]]:
    """
    Fetch the newest `limit` inbox messages.
    When mark_as_read=True (default), fetched messages get the \\Seen flag.
    """
    account = _account()
    limit = max(1, min(int(limit), 50))
    mail = imaplib.IMAP4_SSL(account["imap_server"])
    results: list[dict[str, Any]] = []

    try:
        mail.login(account["email"], account["app_password"])
        mail.select("inbox", readonly=False)

        criteria = "UNSEEN" if unread_only else "ALL"
        status, messages = mail.search(None, criteria)
        if status != "OK" or not messages or not messages[0]:
            return []

        mail_ids = messages[0].split()
        last_ids = list(reversed(mail_ids[-limit:]))

        for mail_id in last_ids:
            status, data = mail.fetch(mail_id, "(RFC822)")
            if status != "OK" or not data or not data[0]:
                continue

            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)
            results.append(
                {
                    "from": _decode_header_value(msg.get("From")),
                    "subject": _decode_header_value(msg.get("Subject")),
                    "snippet": _body_snippet(msg),
                    "message_id": msg.get("Message-ID") or mail_id.decode(),
                    "imap_id": mail_id.decode(),
                }
            )

            if mark_as_read:
                mail.store(mail_id, "+FLAGS", "\\Seen")

    finally:
        try:
            mail.logout()
        except Exception:  # noqa: BLE001
            pass

    return results


def send_email(
    subject: str,
    body: str,
    to_addr: str | None = None,
    *,
    from_addr: str | None = None,
) -> dict[str, Any]:
    """Send a plain-text email via Gmail SMTP."""
    try:
        account = _account()
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}

    recipient = (to_addr or account["email"]).strip()
    sender = (from_addr or account["email"]).strip()
    if not recipient:
        return {"ok": False, "error": "No recipient address."}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    try:
        with smtplib.SMTP(account["smtp_server"], int(account["smtp_port"]), timeout=30) as smtp:
            smtp.starttls()
            smtp.login(account["email"], account["app_password"])
            smtp.send_message(msg)
        return {"ok": True, "to": recipient, "subject": subject}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def read_last_10_emails() -> list[dict[str, Any]]:
    emails = read_last_emails(10)
    for idx, item in enumerate(emails, 1):
        print(f"[{idx}] From: {item['from']}")
        print(f"    Subject: {item['subject']}")
        snippet = item["snippet"]
        print(
            f"    Snippet: {snippet}...\n" if snippet else "    Snippet: (No text body found)\n"
        )
        print("-" * 50)
    return emails


if __name__ == "__main__":
    read_last_10_emails()

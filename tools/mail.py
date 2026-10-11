"""Gmail: reading the inbox, drafting, and sending.

Named mail.py, not email.py, on purpose. A module called email.py in the
project root would shadow Python's own `email` package -- which smtplib
and imaplib both import -- and the failure looks like the standard
library being broken rather than a naming clash.

Why SMTP/IMAP rather than driving the Gmail web page: a sent message can
be *verified*. The agent reads the Sent folder afterwards and confirms
the message is really there. Clicking Send in a browser leaves nothing to
check except a screenshot, and this project does not accept "it looked
like it worked" anywhere else.

Sending is the first genuinely irreversible thing the agent can do. There
is no undo, the recipient is a real person, and a wrong address cannot be
recalled. So `send_email` is IRREVERSIBLE, which means the executor stops
and shows the exact recipient, subject and body before it runs -- the
real text, resolved, not a placeholder.
"""

from __future__ import annotations

import imaplib
import os
import re
import smtplib
import time
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

from core import config  # noqa: F401  -- importing loads .env
from core.schemas import Risk
from tools.registry import tool

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

SENT_FOLDER = "[Gmail]/Sent Mail"
DRAFTS_FOLDER = "[Gmail]/Drafts"

# Gmail rejects an ordinary account password over SMTP. An app password
# is a 16-character token generated per application, revocable on its
# own, and useless for signing in to the account itself -- which is why
# it is the right credential to hand an agent.
_SETUP_HELP = (
    "Gmail is not configured. Add to .env:\n"
    "  GMAIL_ADDRESS=you@gmail.com\n"
    "  GMAIL_APP_PASSWORD=your_16_character_app_password\n"
    "Create an app password at https://myaccount.google.com/apppasswords "
    "(needs 2-step verification enabled). Do not use your normal password."
)


class MailError(RuntimeError):
    """Gmail could not be reached, or a message could not be handled."""


def _is_configured() -> bool:
    return bool(
        os.getenv("GMAIL_ADDRESS", "").strip()
        and os.getenv("GMAIL_APP_PASSWORD", "").strip()
    )


def _mail_tool(risk: Risk):
    """Register a mail tool only when Gmail is actually set up.

    A registry should describe what the agent can really do. Registered
    unconditionally, these tools appear in the catalog on a machine with
    no credentials -- so the planner plans them and, worse, the verifier
    picks one as a probe. That happened: a browser-composed email was
    verified correctly at every step, and then `find_sent` was chosen to
    check the last one, could not connect, and failed a step whose work
    had succeeded.

    An unusable tool in the catalog is not a missing feature, it is a
    false promise, and the verifier believes it.
    """

    def decorator(fn):
        return tool(risk=risk)(fn) if _is_configured() else fn

    return decorator


def _credentials() -> tuple[str, str]:
    address = os.getenv("GMAIL_ADDRESS", "").strip()
    password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not address or not password:
        raise MailError(_SETUP_HELP)
    return address, password


def _imap() -> imaplib.IMAP4_SSL:
    address, password = _credentials()
    try:
        connection = imaplib.IMAP4_SSL(IMAP_HOST)
        connection.login(address, password)
        return connection
    except imaplib.IMAP4.error as exc:
        # Never echo the password back, not even masked. A credential in
        # an error message ends up in logs, in the model's context, and
        # eventually in a transcript someone pastes somewhere.
        raise MailError(
            f"Gmail rejected the login for {address}. Check the app password "
            f"and that IMAP is enabled in Gmail settings. ({exc})"
        ) from exc
    except OSError as exc:
        raise MailError(f"Could not reach {IMAP_HOST}: {exc}") from exc


def _decode(raw: str | None) -> str:
    """Decode an email header into readable text."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _body_of(message) -> str:
    """Best-effort plain text of a message."""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8",
                                      errors="replace")
        return ""
    payload = message.get_payload(decode=True) or b""
    return payload.decode(message.get_content_charset() or "utf-8", errors="replace")


def _fetch(connection, folder: str, criteria: str, limit: int) -> list[dict]:
    import email as email_pkg

    status, _ = connection.select(f'"{folder}"', readonly=True)
    if status != "OK":
        raise MailError(f"Could not open the folder {folder!r}.")

    status, data = connection.search(None, criteria)
    if status != "OK":
        raise MailError(f"Search failed: {criteria}")

    ids = (data[0] or b"").split()
    messages = []
    for message_id in reversed(ids[-limit:]):  # newest first
        status, raw = connection.fetch(message_id, "(RFC822)")
        if status != "OK" or not raw or not raw[0]:
            continue
        parsed = email_pkg.message_from_bytes(raw[0][1])
        try:
            when = parsedate_to_datetime(parsed.get("Date")).strftime("%Y-%m-%d %H:%M")
        except Exception:
            when = parsed.get("Date", "")
        messages.append(
            {
                "from": _decode(parsed.get("From")),
                "to": _decode(parsed.get("To")),
                "subject": _decode(parsed.get("Subject")),
                "date": when,
                "body": _body_of(parsed).strip(),
            }
        )
    return messages


@_mail_tool(Risk.READ)
def search_inbox(query: str = "", limit: int = 5) -> str:
    """Search the Gmail inbox and return matching messages, newest first.

    `query` searches sender, subject and body; leave it empty for the
    most recent messages. Returns sender, date, subject and the first
    part of each body. Use this to find a message before replying to it
    or acting on it.
    """
    limit = max(1, min(limit, 20))
    criteria = f'(TEXT "{query.strip()}")' if query.strip() else "ALL"

    connection = _imap()
    try:
        messages = _fetch(connection, "INBOX", criteria, limit)
    finally:
        try:
            connection.logout()
        except Exception:
            pass

    if not messages:
        return f"No messages found for {query!r}." if query else "The inbox is empty."

    blocks = []
    for message in messages:
        body = message["body"]
        if len(body) > 600:
            body = body[:600] + "\n... [truncated]"
        blocks.append(
            f"From: {message['from']}\nDate: {message['date']}\n"
            f"Subject: {message['subject']}\n\n{body}"
        )
    return f"{len(blocks)} message(s):\n\n" + "\n\n---\n\n".join(blocks)


@_mail_tool(Risk.WRITE)
def save_draft(to: str, subject: str, body: str) -> str:
    """Save an email as a Gmail draft without sending it.

    Use this when the task is to prepare an email rather than send one.
    The draft appears in Gmail where a person can read, edit and send it
    themselves. Nothing leaves the account.
    """
    address, _ = _credentials()
    _require_address(to)

    message = EmailMessage()
    message["From"] = address
    message["To"] = to.strip()
    message["Subject"] = subject.strip()
    message.set_content(body)

    connection = _imap()
    try:
        connection.append(
            f'"{DRAFTS_FOLDER}"', "\\Draft",
            imaplib.Time2Internaldate(time.time()), message.as_bytes(),
        )
    except Exception as exc:
        raise MailError(f"Could not save the draft: {exc}") from exc
    finally:
        try:
            connection.logout()
        except Exception:
            pass

    return (
        f"Saved a draft to {to.strip()} with subject {subject.strip()!r} "
        f"({len(body.split())} words). It has NOT been sent."
    )


@_mail_tool(Risk.IRREVERSIBLE)
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email. This cannot be undone, so it always needs approval first.

    The recipient receives it immediately and there is no recall. Use
    save_draft instead when the task only asks for an email to be
    prepared. Returns the subject and time, which find_sent can confirm.
    """
    address, password = _credentials()
    _require_address(to)
    if not subject.strip():
        raise MailError("An email needs a subject.")

    message = EmailMessage()
    message["From"] = address
    message["To"] = to.strip()
    message["Subject"] = subject.strip()
    message.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(address, password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(
            f"Gmail rejected the login for {address}. Check the app password. ({exc.smtp_code})"
        ) from exc
    except Exception as exc:
        raise MailError(f"Could not send: {type(exc).__name__}: {exc}") from exc

    return (
        f"Sent to {to.strip()} with subject {subject.strip()!r} at "
        f"{time.strftime('%Y-%m-%d %H:%M')}."
    )


@_mail_tool(Risk.READ)
def find_sent(subject: str, limit: int = 5) -> str:
    """Check the Gmail Sent folder for a message with this subject.

    This is how a send is verified: the message is either in Sent or it
    is not. Use it as the check after sending -- the send tool's own
    return value is a claim, and this reads the account back.
    """
    connection = _imap()
    try:
        messages = _fetch(
            connection, SENT_FOLDER,
            f'(SUBJECT "{subject.strip()}")' if subject.strip() else "ALL",
            max(1, min(limit, 20)),
        )
    finally:
        try:
            connection.logout()
        except Exception:
            pass

    if not messages:
        return f"No sent message found with subject {subject.strip()!r}."

    lines = [f"{len(messages)} matching sent message(s):"]
    for message in messages:
        lines.append(
            f"  to {message['to']} on {message['date']} -- {message['subject']!r}"
        )
    return "\n".join(lines)


def _require_address(value: str) -> None:
    """Reject anything that is not a single plausible email address.

    A recipient is the one argument where being wrong is unrecoverable,
    and it is exactly the field a placeholder or a stray sentence is
    most likely to end up in.
    """
    address = value.strip()
    if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}", address):
        raise MailError(
            f"{value!r} is not a single valid email address. "
            "Give exactly one recipient, like name@example.com."
        )

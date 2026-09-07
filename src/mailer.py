"""Component 4 — Mailer.

Sends the HTML digest (from :mod:`digest_builder`) to the owner's Gmail via
Gmail SMTP + an App Password. No Gmail API, no OAuth — just ``smtplib`` over
STARTTLS on ``smtp.gmail.com:587``.

Credentials come from environment variables only, never hardcoded:

    GMAIL_ADDRESS         the account that sends (also the ``From:``)
    GMAIL_APP_PASSWORD    16-char App Password, NOT the normal password
    DIGEST_RECIPIENT      where the digest is delivered

Locally these live in ``.env`` (gitignored) and are loaded with
python-dotenv; in production they are GitHub Actions repo secrets.

Usage::

    # compose only — prints the MIME message, never touches SMTP
    python src/mailer.py --html data/preview.html --to me@gmail.com --dry-run

    # actually send
    python src/mailer.py --html data/preview.html

The App Password is never logged or printed, including in error messages.
"""

from __future__ import annotations

import argparse
import html as _htmlmod
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, formataddr
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mailer")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_TIMEOUT = 30  # seconds — every network call is bounded

SENDER_NAME = "AI Industry Token Bites"
SUBJECT_PREFIX = "AI Industry Token Bites — Week of "
LOOKBACK_DAYS = 7  # only used for the subject-line fallback date range


class MailerError(RuntimeError):
    """Raised for any unrecoverable mailer problem (config or send)."""


# --------------------------------------------------------------------------- #
# subject / date
# --------------------------------------------------------------------------- #

# digest_builder writes  <title>AI Industry Token Bites — Week of {range}</title>
# and a  "Week of {range}"  line in the header. Pull the range straight back
# out of the rendered HTML so the subject matches the body exactly.
_WEEK_OF_RE = re.compile(r"Week of\s+([^<\n]+?)\s*(?:<|\n|$)")


def _fallback_range() -> str:
    """A 'Aug 30–Sep 6, 2026'-style range ending today (subject fallback)."""
    end = datetime.now()
    start = end - timedelta(days=LOOKBACK_DAYS)
    if (start.month, start.year) == (end.month, end.year):
        return f"{start:%b} {start.day}–{end.day}, {end:%Y}"
    if start.year == end.year:
        return f"{start:%b} {start.day} – {end:%b} {end.day}, {end:%Y}"
    return f"{start:%b} {start.day}, {start:%Y} – {end:%b} {end.day}, {end:%Y}"


def subject_for(html_body: str) -> str:
    """Build the subject line, taking the date range from the HTML if present."""
    m = _WEEK_OF_RE.search(html_body)
    date_range = m.group(1).strip() if m else _fallback_range()
    return SUBJECT_PREFIX + date_range


# --------------------------------------------------------------------------- #
# plain-text alternative
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_BLANKS_RE = re.compile(r"\n\s*\n\s*\n+")


def html_to_text(html_body: str) -> str:
    """Rough text rendering for the ``text/plain`` alternative part.

    Not a full HTML renderer — just enough that a text-only client shows
    something readable instead of raw markup.
    """
    text = re.sub(r"(?is)<(head|style|script|title)[^>]*>.*?</\1>", "", html_body)
    text = re.sub(r"(?i)</(p|div|h1|h2|h3|tr|table|li)>", "\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = _TAG_RE.sub("", text)
    text = _htmlmod.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANKS_RE.sub("\n\n", text).strip()
    return text or "This digest has an HTML body only."


# --------------------------------------------------------------------------- #
# compose
# --------------------------------------------------------------------------- #


def compose(html_body: str, *, sender: str, recipient: str,
            subject: str | None = None) -> EmailMessage:
    """Build the multipart/alternative message (text + HTML)."""
    msg = EmailMessage()
    msg["Subject"] = subject or subject_for(html_body)
    msg["From"] = formataddr((SENDER_NAME, sender))
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or None)
    msg.set_content(html_to_text(html_body))
    msg.add_alternative(html_body, subtype="html")
    return msg


# --------------------------------------------------------------------------- #
# send
# --------------------------------------------------------------------------- #


def send(msg: EmailMessage, *, sender: str, app_password: str) -> None:
    """Deliver ``msg`` through Gmail SMTP. Raises :class:`MailerError` on failure.

    The App Password is only ever passed to ``smtplib`` — it is never logged,
    printed, or placed in an exception message.
    """
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(sender, app_password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise MailerError(
            "SMTP authentication failed. The most likely cause is a wrong or "
            "revoked GMAIL_APP_PASSWORD — it must be a 16-character Gmail App "
            "Password (https://myaccount.google.com/apppasswords, 2FA required), "
            "not your normal account password. Also confirm GMAIL_ADDRESS is the "
            f"account that owns that App Password. (server said: {e.smtp_code})"
        ) from None
    except smtplib.SMTPRecipientsRefused as e:
        raise MailerError(f"recipient refused by server: {list(e.recipients)}") from None
    except smtplib.SMTPException as e:
        raise MailerError(f"SMTP error while sending: {e.__class__.__name__}: {e}") from None
    except (OSError, TimeoutError) as e:
        raise MailerError(
            f"could not reach {SMTP_HOST}:{SMTP_PORT}: {e.__class__.__name__}: {e}"
        ) from None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _env(name: str) -> str | None:
    val = os.environ.get(name)
    return val.strip() if val else None


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Email the HTML digest via Gmail SMTP.")
    parser.add_argument("--html", type=Path, required=True,
                        help="path to the HTML file to send")
    parser.add_argument("--to", metavar="ADDRESS",
                        help="override DIGEST_RECIPIENT (for testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the composed MIME message and exit; no SMTP call")
    args = parser.parse_args(argv)

    try:
        html_body = args.html.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: cannot read {args.html}: {e}", file=sys.stderr)
        return 1
    if not html_body.strip():
        print(f"error: {args.html} is empty", file=sys.stderr)
        return 1

    sender = _env("GMAIL_ADDRESS")
    recipient = args.to or _env("DIGEST_RECIPIENT") or sender

    missing = []
    if not sender:
        missing.append("GMAIL_ADDRESS")
    if not recipient:
        missing.append("DIGEST_RECIPIENT (or pass --to)")
    if missing:
        print(f"error: missing required config: {', '.join(missing)} "
              f"(set in .env locally, repo secrets in production)", file=sys.stderr)
        return 1

    try:
        msg = compose(html_body, sender=sender, recipient=recipient)
    except ValueError as e:
        print(f"error: could not compose message: {e}", file=sys.stderr)
        return 1

    if args.dry_run:
        log.info("dry run — composed message follows, SMTP not contacted")
        print(msg.as_string())
        return 0

    app_password = _env("GMAIL_APP_PASSWORD")
    if not app_password:
        print("error: missing required config: GMAIL_APP_PASSWORD "
              "(Gmail App Password, set in .env locally)", file=sys.stderr)
        return 1

    log.info("sending digest to %s (subject: %r)", recipient, msg["Subject"])
    try:
        send(msg, sender=sender, app_password=app_password)
    except MailerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    log.info("digest delivered to %s", recipient)
    return 0


if __name__ == "__main__":
    sys.exit(_main())

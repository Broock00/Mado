"""Sending email.

Two senders behind one interface, chosen by whether SMTP is configured.

**Why an outbox rather than a stub that silently succeeds.** Verification and
password reset are useless if the message does not arrive, and the failure mode
of a no-op sender is that everything looks fine until a real person is locked
out of their account. With no SMTP host configured, development writes each
message to a local directory that stands in for an inbox - the flows can be
completed by opening the file - and every other environment logs a warning,
because running without a relay anywhere real is a misconfiguration.

**Why SMTP rather than a vendor SDK.** Spec 82.01 treats external services as
replaceable infrastructure. Every transactional provider speaks SMTP, so this
runs against Postmark, SES, Mailgun or a local MailHog without a code change.

Sending happens off the request path. An explorer registering an account should
not wait on a mail server, and a slow relay must not turn into a slow API - so
the send is dispatched as a task and its failure is logged rather than raised.
The token is already in the database by then, and a link that fails to send is
recoverable by asking again.
"""

from __future__ import annotations

import asyncio
import pathlib
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("mado.email")


@dataclass(slots=True)
class Message:
    to: str
    subject: str
    body: str


class OutboxSender:
    """Writes messages to a local directory instead of sending them.

    Used whenever SMTP is not configured. In development that directory *is* the
    inbox: verification and reset flows can be completed by opening the file and
    following the link, so the whole feature works end to end with no relay.

    The body is written to disk rather than logged because the log redactor
    scrubs anything shaped like a credential - correctly, since a reset link is
    exactly that. Weakening the redactor to make development convenient would
    trade a real protection for a small one, so the link goes somewhere the
    redactor is not, and the log gets only a path.

    Outside development only a warning is emitted, with no body. Running without
    SMTP anywhere real is a misconfiguration, and the right response is a loud
    log entry rather than a folder quietly filling with unsent password resets.
    """

    def __init__(self, settings) -> None:
        self.development = settings.environment == "development"
        self.directory = pathlib.Path(settings.media_root).parent / "outbox"

    async def send(self, message: Message) -> None:
        if not self.development:
            logger.warning("email_not_sent_no_smtp", to=message.to, subject=message.subject)
            return

        await asyncio.to_thread(self._write, message)

    def _write(self, message: Message) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        path = self.directory / f"{stamp}-{message.to.replace('@', '_at_')}.txt"
        path.write_text(
            f"To: {message.to}\nSubject: {message.subject}\n\n{message.body}",
            encoding="utf-8",
        )
        logger.info("email_written_to_outbox", to=message.to, path=str(path))


class SmtpSender:
    def __init__(self, settings) -> None:
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.username = settings.smtp_username
        self.password = settings.smtp_password
        self.use_tls = settings.smtp_use_tls
        self.sender = settings.email_from

    async def send(self, message: Message) -> None:
        # smtplib is blocking, so it goes to a worker thread. A mail relay having
        # a slow day must not hold an event loop worker hostage.
        await asyncio.to_thread(self._send_blocking, message)

    def _send_blocking(self, message: Message) -> None:
        email = EmailMessage()
        email["From"] = self.sender
        email["To"] = message.to
        email["Subject"] = message.subject
        email.set_content(message.body)

        with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username and self.password:
                smtp.login(self.username, self.password)
            smtp.send_message(email)


def get_sender():
    settings = get_settings()
    if settings.smtp_host:
        return SmtpSender(settings)
    return OutboxSender(settings)


async def send(message: Message) -> None:
    """Send, treating failure as loggable rather than fatal.

    The caller has already committed whatever the email is about. Raising here
    would roll back a verification token that is perfectly valid, leaving the
    explorer unable even to ask for a new one.
    """
    try:
        await get_sender().send(message)
    except Exception as exc:  # noqa: BLE001
        logger.warning("email_send_failed", to=message.to, error=str(exc))


def dispatch(message: Message) -> None:
    """Fire-and-forget, so the request does not wait on a mail server."""
    task = asyncio.create_task(send(message))
    # Held until done: asyncio only keeps a weak reference, and a task that is
    # garbage collected mid-flight is an email that silently never sends.
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


_PENDING: set[asyncio.Task] = set()


# ------------------------------------------------------------------ messages
#
# Plain text, deliberately. An HTML template is another thing to maintain, and
# the two emails this platform sends are one sentence and one link each.


def verification_message(to: str, name: str, link: str) -> Message:
    return Message(
        to=to,
        subject="Confirm your email for Mado",
        body=(
            f"Hello {name},\n\n"
            "Confirm this address to finish setting up your Mado account:\n\n"
            f"{link}\n\n"
            "The link works for one day. If you did not create an account, you "
            "can ignore this - nothing will happen without it.\n"
        ),
    )


def reset_message(to: str, name: str, link: str) -> Message:
    return Message(
        to=to,
        subject="Reset your Mado password",
        body=(
            f"Hello {name},\n\n"
            "Use this link to choose a new password:\n\n"
            f"{link}\n\n"
            "The link works for one hour and only once. If you did not ask for "
            "this, ignore it - your password has not changed, and whoever asked "
            "cannot see this message.\n"
        ),
    )


def password_changed_message(to: str, name: str) -> Message:
    """Told after the fact, not asked for permission.

    If somebody else changed the password, this is how the owner finds out while
    it is still recoverable. It is the single most useful security email a
    platform sends, and it costs nothing.
    """
    return Message(
        to=to,
        subject="Your Mado password was changed",
        body=(
            f"Hello {name},\n\n"
            "The password on your Mado account was just changed, and every other "
            "signed-in device was signed out.\n\n"
            "If that was you, there is nothing to do. If it was not, reset your "
            "password immediately - the link on the sign-in page will send a new "
            "one to this address.\n"
        ),
    )

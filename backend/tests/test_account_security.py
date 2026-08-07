"""Email verification, password reset and session tests.

These flows are the ones an attacker actually goes after, so the tests are
mostly about what the code refuses to do: leak who has an account, accept a
spent link, or leave a stolen session alive through a password reset.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.errors import ValidationError
from app.domains.identity.schemas import MIN_PASSWORD_LENGTH, check_password_strength
from app.domains.identity.tokens import (
    PURPOSE_RESET_PASSWORD,
    PURPOSE_VERIFY_EMAIL,
    RESET_TTL,
    VERIFY_TTL,
    AccountTokenService,
    _digest,
)

pytestmark = pytest.mark.anyio


class FakeSession:
    """Enough of an AsyncSession for the token service.

    The service's logic is entirely about time, purpose and single use - none of
    which SQLAlchemy decides - so the rows live in a list.
    """

    def __init__(self) -> None:
        self.rows: list = []

    def add(self, row) -> None:
        self.rows.append(row)

    async def flush(self) -> None:
        pass

    async def execute(self, statement):
        # The service issues exactly two shapes of query: one by digest, one by
        # (user, purpose, unused). The bound parameters say which.
        params = statement.compile().params
        if "token_hash_1" in params:
            matches = [r for r in self.rows if r.token_hash == params["token_hash_1"]]
        else:
            matches = [
                r
                for r in self.rows
                if r.user_id == params.get("user_id_1")
                and r.purpose == params.get("purpose_1")
                and r.used_at is None
            ]
        return SimpleNamespace(
            scalar_one_or_none=lambda: matches[0] if matches else None,
            scalars=lambda: iter(matches),
        )


def service() -> AccountTokenService:
    return AccountTokenService(FakeSession())  # type: ignore[arg-type]


class TestTokensAtRest:
    async def test_the_plaintext_is_never_stored(self):
        """A database read must not yield a working reset link."""
        svc = service()
        token = await svc.issue(uuid.uuid4(), PURPOSE_RESET_PASSWORD)
        stored = svc.session.rows[0].token_hash  # type: ignore[attr-defined]
        assert token not in stored
        assert stored == _digest(token)

    async def test_two_issues_never_collide(self):
        svc = service()
        user = uuid.uuid4()
        first = await svc.issue(user, PURPOSE_VERIFY_EMAIL)
        second = await svc.issue(user, PURPOSE_VERIFY_EMAIL)
        assert first != second

    async def test_the_token_is_long_enough_to_be_unguessable(self):
        token = await service().issue(uuid.uuid4(), PURPOSE_RESET_PASSWORD)
        assert len(token) >= 32


class TestSingleUse:
    async def test_a_token_works_once(self):
        svc = service()
        user = uuid.uuid4()
        token = await svc.issue(user, PURPOSE_VERIFY_EMAIL)
        assert await svc.consume(token, PURPOSE_VERIFY_EMAIL) == user

    async def test_and_not_twice(self):
        svc = service()
        token = await svc.issue(uuid.uuid4(), PURPOSE_VERIFY_EMAIL)
        await svc.consume(token, PURPOSE_VERIFY_EMAIL)
        with pytest.raises(ValidationError):
            await svc.consume(token, PURPOSE_VERIFY_EMAIL)

    async def test_asking_again_invalidates_the_previous_link(self):
        """Two live reset links mean two chances for an old one to be found."""
        svc = service()
        user = uuid.uuid4()
        first = await svc.issue(user, PURPOSE_RESET_PASSWORD)
        await svc.issue(user, PURPOSE_RESET_PASSWORD)
        with pytest.raises(ValidationError):
            await svc.consume(first, PURPOSE_RESET_PASSWORD)

    async def test_the_newest_link_is_the_one_that_works(self):
        svc = service()
        user = uuid.uuid4()
        await svc.issue(user, PURPOSE_RESET_PASSWORD)
        newest = await svc.issue(user, PURPOSE_RESET_PASSWORD)
        assert await svc.consume(newest, PURPOSE_RESET_PASSWORD) == user


class TestPurposeSeparation:
    async def test_a_verification_link_cannot_reset_a_password(self):
        """Otherwise the weaker, longer-lived token becomes the stronger one."""
        svc = service()
        token = await svc.issue(uuid.uuid4(), PURPOSE_VERIFY_EMAIL)
        with pytest.raises(ValidationError):
            await svc.consume(token, PURPOSE_RESET_PASSWORD)

    async def test_and_the_failed_attempt_does_not_spend_it(self):
        """A wrong-purpose probe must not become a denial of service."""
        svc = service()
        user = uuid.uuid4()
        token = await svc.issue(user, PURPOSE_VERIFY_EMAIL)
        with pytest.raises(ValidationError):
            await svc.consume(token, PURPOSE_RESET_PASSWORD)
        assert await svc.consume(token, PURPOSE_VERIFY_EMAIL) == user


class TestExpiry:
    async def test_an_expired_link_is_refused(self):
        svc = service()
        token = await svc.issue(uuid.uuid4(), PURPOSE_RESET_PASSWORD)
        svc.session.rows[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)  # type: ignore[attr-defined]
        with pytest.raises(ValidationError):
            await svc.consume(token, PURPOSE_RESET_PASSWORD)

    def test_reset_links_die_sooner_than_verification_links(self):
        """A reset link is a key to the account; a verification link is not."""
        assert RESET_TTL < VERIFY_TTL

    def test_a_reset_link_does_not_linger_in_a_mailbox(self):
        assert timedelta(hours=2) >= RESET_TTL

    def test_a_verification_link_survives_a_cluttered_inbox(self):
        assert timedelta(hours=12) <= VERIFY_TTL


class TestFailuresAreIndistinguishable:
    """Unknown, spent, expired and wrong-purpose all answer the same.

    The distinctions are real, but reporting which one applies only helps
    somebody probing with tokens they were never sent.
    """

    async def test_every_rejection_carries_the_same_code(self):
        svc = service()
        user = uuid.uuid4()

        spent = await svc.issue(user, PURPOSE_VERIFY_EMAIL)
        await svc.consume(spent, PURPOSE_VERIFY_EMAIL)

        expired = await svc.issue(user, PURPOSE_RESET_PASSWORD)
        svc.session.rows[-1].expires_at = datetime.now(UTC) - timedelta(seconds=1)  # type: ignore[attr-defined]

        codes = set()
        for token, purpose in [
            ("never-existed", PURPOSE_VERIFY_EMAIL),
            (spent, PURPOSE_VERIFY_EMAIL),
            (expired, PURPOSE_RESET_PASSWORD),
            (spent, PURPOSE_RESET_PASSWORD),
        ]:
            with pytest.raises(ValidationError) as caught:
                await svc.consume(token, purpose)
            codes.add(caught.value.code)

        assert codes == {"TOKEN_INVALID"}


class TestPasswordFloor:
    """The same rule at signup, at reset and at change.

    A floor that applies only at registration is not a floor - anyone can step
    around it by asking for a reset link.
    """

    def test_letters_alone_are_refused(self):
        with pytest.raises(ValueError, match="letters with numbers"):
            check_password_strength("justletters")

    def test_digits_alone_are_refused(self):
        with pytest.raises(ValueError, match="letters with numbers"):
            check_password_strength("1234567890")

    def test_padding_with_whitespace_is_refused(self):
        """Trailing spaces get eaten by forms and lock people out."""
        with pytest.raises(ValueError, match="whitespace"):
            check_password_strength(" secret-p4ss ")

    def test_a_reasonable_password_passes(self):
        assert check_password_strength("Str0ng-Passw0rd!") == "Str0ng-Passw0rd!"

    def test_length_carries_the_requirement(self):
        assert MIN_PASSWORD_LENGTH >= 10


class TestEmailDoesNotBlockTheRequest:
    async def test_a_failing_sender_is_logged_rather_than_raised(self):
        """The token is already committed by the time the mail is attempted.

        Raising would roll back a perfectly valid verification token, leaving the
        explorer unable even to ask for a new one.
        """
        from app.integrations import email as mailer

        class Broken:
            async def send(self, _message):
                raise RuntimeError("relay refused")

        original = mailer.get_sender
        mailer.get_sender = lambda: Broken()
        try:
            await mailer.send(mailer.Message(to="a@b.test", subject="s", body="b"))
        finally:
            mailer.get_sender = original

    def test_outbox_only_writes_bodies_in_development(self):
        """Elsewhere a missing relay is a misconfiguration, not a delivery channel."""
        from app.integrations.email import OutboxSender

        settings = SimpleNamespace(environment="production", media_root="var/media")
        assert OutboxSender(settings).development is False
        settings = SimpleNamespace(environment="development", media_root="var/media")
        assert OutboxSender(settings).development is True

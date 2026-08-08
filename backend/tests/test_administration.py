"""Administration and publisher verification tests.

Almost everything here is about what an administrator is *stopped* from doing.
The happy paths - suspend someone, verify a publisher - are one assignment each
and fail loudly. The guards are the part that has to hold: a moderator who
suspends themselves has locked the only door, and a verification badge that can
be granted without a person deciding is worth nothing.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.models  # noqa: F401  (audit entries are built here, so mappers must resolve)
from app.core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from app.domains.trust.administration import (
    STATUS_ACTIVE,
    STATUS_SUSPENDED,
    TRUST_ON_VERIFICATION,
    VERIFICATION_REQUESTED,
    VERIFICATION_UNVERIFIED,
    VERIFICATION_VERIFIED,
    AdministrationService,
    require_moderator,
)

pytestmark = pytest.mark.anyio


def account(**overrides):
    """A stand-in for a User row, with only the columns this service touches."""
    return SimpleNamespace(
        **{
            "id": uuid.uuid4(),
            "status": STATUS_ACTIVE,
            "is_moderator": False,
            # The service labels audit entries from here. Present on the stub
            # because it is present on the row: `_load` eager-loads it.
            "profile": SimpleNamespace(display_name="Someone"),
            **overrides,
        }
    )


def publisher(**overrides):
    return SimpleNamespace(
        **{
            "id": uuid.uuid4(),
            "name": "A publisher",
            "verification_status": VERIFICATION_UNVERIFIED,
            "verification_note": None,
            "verification_requested_at": None,
            "verification_decided_at": None,
            "trust_level": 0,
            **overrides,
        }
    )


def recording_session():
    """A session that only collects what was added.

    Enough for these tests because every administrative action now writes an
    audit entry through the caller's session, and holding on to them lets a test
    assert the record exists rather than only the state change.
    """
    added: list = []
    return SimpleNamespace(add=added.append, added=added)


def service_over(*accounts, publisher_row=None) -> AdministrationService:
    """A service whose loads are satisfied from memory rather than a database.

    The guards under test are decisions about in-memory state; wiring a real
    session in would test SQLAlchemy, which is not where these bugs live.
    """
    by_id = {a.id: a for a in accounts}
    service = AdministrationService(session=recording_session())

    async def _load(user_id):
        found = by_id.get(user_id)
        if found is None:
            raise NotFoundError("Account not found.", code="ACCOUNT_NOT_FOUND")
        return found

    async def _publisher_for(_user):
        if publisher_row is None:
            raise NotFoundError("No publisher.", code="NO_PUBLISHER")
        return publisher_row

    service._load = _load  # type: ignore[method-assign]
    service._publisher_for = _publisher_for  # type: ignore[method-assign]
    return service


class TestEveryDecisionLeavesARecord:
    """Spec ADM-004. The point of the audit log is that it is not optional.

    Asserted at this level rather than in the audit tests because the failure
    being guarded against is a new administrative action shipping without one -
    which the audit module cannot notice on its own.
    """

    async def test_a_suspension_is_recorded_with_its_reason(self):
        actor, target = account(is_moderator=True), account()
        service = service_over(actor, target)
        await service.set_suspended(
            actor, target.id, suspended=True, reason="Repeated scam listings"
        )

        entry = only_entry(service)
        assert entry.action == "account.suspended"
        assert entry.subject_id == target.id
        assert entry.reason == "Repeated scam listings"

    async def test_lifting_one_is_a_separate_action_not_the_same_one_again(self):
        """"Status changed" would need the context read to know which happened."""
        actor = account(is_moderator=True)
        target = account(status=STATUS_SUSPENDED)
        service = service_over(actor, target)
        await service.set_suspended(actor, target.id, suspended=False)
        assert only_entry(service).action == "account.restored"

    async def test_granting_moderator_rights_is_recorded(self):
        actor, target = account(is_moderator=True), account()
        service = service_over(actor, target)
        await service.set_moderator(actor, target.id, moderator=True)
        assert only_entry(service).action == "moderator.granted"

    async def test_a_verification_decision_is_recorded_against_the_publisher(self):
        row = publisher(verification_status=VERIFICATION_REQUESTED)
        service = TestVerificationDecisions().service_for(row)
        await service.decide_verification(account(is_moderator=True), row.id, approve=False)

        entry = only_entry(service)
        assert entry.action == "verification.refused"
        assert entry.subject_type == "publisher"
        assert entry.subject_label == row.name

    async def test_a_refused_action_records_nothing(self):
        """A guard that fired means the action did not happen, and a record of
        something that did not happen is worse than no record."""
        actor = account(is_moderator=True)
        service = service_over(actor)
        with pytest.raises(ConflictError):
            await service.set_suspended(actor, actor.id, suspended=True)
        assert service.session.added == []


def only_entry(service):
    assert len(service.session.added) == 1
    return service.session.added[0]


class TestSuspension:
    async def test_suspending_sets_the_status(self):
        actor, target = account(is_moderator=True), account()
        await service_over(actor, target).set_suspended(actor, target.id, suspended=True)
        assert target.status == STATUS_SUSPENDED

    async def test_lifting_a_suspension_restores_the_account(self):
        actor = account(is_moderator=True)
        target = account(status=STATUS_SUSPENDED)
        await service_over(actor, target).set_suspended(actor, target.id, suspended=False)
        assert target.status == STATUS_ACTIVE

    async def test_an_administrator_cannot_suspend_themselves(self):
        """Otherwise they cannot undo it - there is nobody left to lift it."""
        actor = account(is_moderator=True)
        with pytest.raises(ConflictError) as caught:
            await service_over(actor).set_suspended(actor, actor.id, suspended=True)
        assert caught.value.code == "CANNOT_SUSPEND_SELF"

    async def test_a_moderator_must_be_demoted_before_being_suspended(self):
        """Privileges are removed deliberately, never as a side effect."""
        actor, target = account(is_moderator=True), account(is_moderator=True)
        with pytest.raises(ConflictError) as caught:
            await service_over(actor, target).set_suspended(actor, target.id, suspended=True)
        assert caught.value.code == "MODERATOR_MUST_BE_DEMOTED"

    async def test_suspension_does_not_delete_anything(self):
        """Spec BUSINESS-07: withhold, do not destroy.

        The account keeps its posts and its history; a suspension made in error
        should cost the person nothing once it is lifted.
        """
        actor, target = account(is_moderator=True), account()
        target.deleted_at = None
        await service_over(actor, target).set_suspended(actor, target.id, suspended=True)
        assert target.deleted_at is None

    async def test_suspending_an_account_that_does_not_exist_is_a_404(self):
        actor = account(is_moderator=True)
        with pytest.raises(NotFoundError):
            await service_over(actor).set_suspended(actor, uuid.uuid4(), suspended=True)


class TestModeratorRights:
    async def test_granting_moderator_rights(self):
        actor, target = account(is_moderator=True), account()
        await service_over(actor, target).set_moderator(actor, target.id, moderator=True)
        assert target.is_moderator

    async def test_an_administrator_cannot_demote_themselves(self):
        actor = account(is_moderator=True)
        with pytest.raises(ConflictError) as caught:
            await service_over(actor).set_moderator(actor, actor.id, moderator=False)
        assert caught.value.code == "CANNOT_DEMOTE_SELF"

    async def test_a_suspended_account_cannot_be_made_a_moderator(self):
        actor = account(is_moderator=True)
        target = account(status=STATUS_SUSPENDED)
        with pytest.raises(ConflictError) as caught:
            await service_over(actor, target).set_moderator(actor, target.id, moderator=True)
        assert caught.value.code == "ACCOUNT_SUSPENDED"

    def test_the_gate_reads_a_dedicated_column(self):
        """Not a profile field the explorer themselves can write."""
        with pytest.raises(PermissionDeniedError) as caught:
            require_moderator(account(is_moderator=False))
        assert caught.value.code == "NOT_A_MODERATOR"

        moderator = account(is_moderator=True)
        assert require_moderator(moderator) is moderator


class TestVerification:
    async def test_requesting_moves_a_publisher_into_the_queue(self):
        row = publisher()
        user = account()
        await service_over(user, publisher_row=row).request_verification(user, note="Our licence")
        assert row.verification_status == VERIFICATION_REQUESTED
        assert row.verification_requested_at is not None
        assert row.verification_note == "Our licence"

    async def test_requesting_never_grants_the_badge_itself(self):
        """There is no automatic path to verified - that is the whole point."""
        row = publisher()
        user = account()
        await service_over(user, publisher_row=row).request_verification(user)
        assert row.verification_status != VERIFICATION_VERIFIED

    async def test_asking_twice_is_refused_rather_than_resetting_the_queue(self):
        row = publisher(verification_status=VERIFICATION_REQUESTED)
        user = account()
        with pytest.raises(ConflictError) as caught:
            await service_over(user, publisher_row=row).request_verification(user)
        assert caught.value.code == "ALREADY_REQUESTED"

    async def test_an_already_verified_publisher_cannot_reapply(self):
        row = publisher(verification_status=VERIFICATION_VERIFIED)
        user = account()
        with pytest.raises(ConflictError) as caught:
            await service_over(user, publisher_row=row).request_verification(user)
        assert caught.value.code == "ALREADY_VERIFIED"

    async def test_a_long_note_is_truncated_rather_than_rejected(self):
        row = publisher()
        user = account()
        await service_over(user, publisher_row=row).request_verification(user, note="x" * 5000)
        assert len(row.verification_note) == 1000

    async def test_an_empty_note_is_stored_as_absent(self):
        row = publisher()
        user = account()
        await service_over(user, publisher_row=row).request_verification(user, note="   ")
        assert row.verification_note is None

    async def test_you_must_have_published_before_asking_to_be_verified(self):
        """Verification applies to a publisher, so there has to be one."""
        user = account()
        with pytest.raises(NotFoundError) as caught:
            await service_over(user).request_verification(user)
        assert caught.value.code == "NO_PUBLISHER"


class TestVerificationDecisions:
    """`decide_verification` loads through the session, so it gets a stub of one."""

    def service_for(self, row):
        service = AdministrationService(session=recording_session())

        async def _get(_model, publisher_id):
            return row if row is not None and row.id == publisher_id else None

        service.session.get = _get  # type: ignore[attr-defined]
        return service

    async def test_approval_grants_the_badge_and_some_trust(self):
        row = publisher(verification_status=VERIFICATION_REQUESTED)
        await self.service_for(row).decide_verification(
            account(is_moderator=True), row.id, approve=True
        )
        assert row.verification_status == VERIFICATION_VERIFIED
        assert row.trust_level == TRUST_ON_VERIFICATION

    async def test_approval_never_lowers_trust_a_publisher_already_earned(self):
        row = publisher(verification_status=VERIFICATION_REQUESTED, trust_level=8)
        await self.service_for(row).decide_verification(
            account(is_moderator=True), row.id, approve=True
        )
        assert row.trust_level == 8

    async def test_refusal_returns_them_to_unverified_not_to_a_dead_end(self):
        """Someone refused for thin evidence should be able to come back."""
        row = publisher(verification_status=VERIFICATION_REQUESTED)
        await self.service_for(row).decide_verification(
            account(is_moderator=True), row.id, approve=False, note="Licence was illegible"
        )
        assert row.verification_status == VERIFICATION_UNVERIFIED
        assert row.trust_level == 0

    async def test_a_refused_publisher_can_ask_again(self):
        row = publisher(verification_status=VERIFICATION_REQUESTED)
        await self.service_for(row).decide_verification(
            account(is_moderator=True), row.id, approve=False
        )
        user = account()
        await service_over(user, publisher_row=row).request_verification(user, note="Better scan")
        assert row.verification_status == VERIFICATION_REQUESTED

    async def test_every_decision_records_when_it_was_made(self):
        row = publisher(verification_status=VERIFICATION_REQUESTED)
        await self.service_for(row).decide_verification(
            account(is_moderator=True), row.id, approve=False
        )
        assert row.verification_decided_at is not None

    async def test_deciding_on_a_publisher_that_does_not_exist_is_a_404(self):
        with pytest.raises(NotFoundError) as caught:
            await self.service_for(None).decide_verification(
                account(is_moderator=True), uuid.uuid4(), approve=True
            )
        assert caught.value.code == "PUBLISHER_NOT_FOUND"


class TestTrustWeight:
    def test_verification_is_a_small_nudge_not_a_ranking_lever(self):
        """It is a claim about identity, not about quality.

        A verified publisher who posts badly should not outrank a good
        unverified one, so the grant stays well below the ceiling.
        """
        assert 0 < TRUST_ON_VERIFICATION <= 5


class TestWhatASuspendedExplorerIsTold:
    """A suspension nobody can see is a bug report waiting to happen."""

    async def test_a_suspended_account_is_told_why_it_is_refused(self):
        from app.api.deps import current_user

        request = SimpleNamespace(state=SimpleNamespace(auth_refusal="suspended"))
        with pytest.raises(PermissionDeniedError) as caught:
            await current_user(request, None)
        assert caught.value.code == "ACCOUNT_SUSPENDED"

    async def test_a_signed_out_visitor_is_told_to_sign_in(self):
        from app.api.deps import current_user

        request = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(AuthenticationError):
            await current_user(request, None)

    async def test_an_active_explorer_passes_through(self):
        from app.api.deps import current_user

        user = account()
        request = SimpleNamespace(state=SimpleNamespace())
        assert await current_user(request, user) is user

"""Travel alerts (spec NOT-003).

The opposite failure from suggestions. There, the risk is interrupting somebody
who did not ask; here, it is staying quiet - an explorer crossing Addis to a
venue that is shut, holding a reservation the app still shows as confirmed.
Most of these tests are about the alert going out, and going out *once*.

Two of them are about order, which is the trap this feature sets. The pending
"starts tonight" reminder and the cancellation alert share a subject, and the
call that withdraws the first will withdraw the second if it runs after it.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.models  # noqa: F401  (Notification rows are built here)
from app.core.messages import CATALOGUE
from app.domains.explorer import alerts
from app.domains.explorer.notifications import (
    ALL_KINDS,
    DEFAULT_ON,
    KIND_TRAVEL_ALERT,
    Preferences,
)

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
TONIGHT = datetime(2026, 8, 11, 17, 0, tzinfo=UTC)  # 20:00 in Addis
EXPERIENCE = uuid.uuid4()
OCCURRENCE = uuid.uuid4()


def profile(**overrides):
    return SimpleNamespace(
        **{
            "preferences": {},
            "timezone": "Africa/Addis_Ababa",
            "language": "en",
            **overrides,
        }
    )


def user(the_profile=None):
    return SimpleNamespace(id=uuid.uuid4(), profile=the_profile or profile())


class Recorder:
    """A NotificationService that records what happened, and in what order."""

    def __init__(self, session=None) -> None:
        self.calls: list[str] = []
        self.scheduled: list[dict] = []

    async def cancel_for_subject(self, subject_id, *, user_id=None) -> int:
        self.calls.append("withdraw")
        return 1

    async def schedule(self, user_id, **kwargs):
        self.calls.append("schedule")
        if kwargs.get("preferences") is not None and not kwargs["preferences"].wants(
            kwargs["kind"]
        ):
            return None
        self.scheduled.append({"user_id": user_id, **kwargs})
        return SimpleNamespace(id=uuid.uuid4())


def harness(monkeypatch, *, reserved=(), planned=(), saved=(), places=0, users=()):
    """Wire `announce_cancellation` to fixed groups and a recording service.

    The three collectors are replaced rather than faked at the SQL level: what
    is worth pinning here is what the function does with the people it found,
    and a hand-built query result tests the mock.
    """
    recorder = Recorder()
    monkeypatch.setattr(alerts, "NotificationService", lambda _session: recorder)

    async def _reserved(_session, _occurrence_id):
        return set(reserved), places

    async def _planned(_session, **_kwargs):
        return set(planned)

    async def _saved(_session, _experience_id):
        return set(saved)

    monkeypatch.setattr(alerts, "_reserved", _reserved)
    monkeypatch.setattr(alerts, "_planned", _planned)
    monkeypatch.setattr(alerts, "_saved", _saved)

    session = SimpleNamespace(
        execute=lambda _stmt: _resolved(SimpleNamespace(scalars=lambda: list(users)))
    )
    return recorder, session


def _resolved(value):
    async def _await():
        return value

    return _await()


async def announce(session, **overrides):
    return await alerts.announce_cancellation(
        session,
        **{
            "experience_id": EXPERIENCE,
            "occurrence_id": OCCURRENCE,
            "title": "Fendika Azmari Bet",
            "starts_at": TONIGHT,
            "now": NOW,
            **overrides,
        },
    )


class TestTheOrderThatMatters:
    async def test_the_stale_reminder_is_withdrawn_before_the_alert_is_sent(
        self, monkeypatch
    ):
        """`cancel_for_subject` cancels everything pending about a subject,
        whatever its kind. The alert has the same subject as the reminder it
        replaces, so scheduling first means the withdrawal cancels the alert
        too - and the explorer hears nothing at all, with no error anywhere."""
        someone = user()
        recorder, session = harness(monkeypatch, saved=[someone.id], users=[someone])

        await announce(session)

        assert recorder.calls == ["withdraw", "schedule"]
        assert recorder.scheduled, "the alert did not survive the withdrawal"

    async def test_the_reminder_is_withdrawn_even_when_nobody_is_told(self, monkeypatch):
        """A date cancelled after it started warns nobody, but the queued
        reminder still has to go - otherwise it fires tonight about an event
        that was called off this morning."""
        recorder, session = harness(monkeypatch)

        told = await announce(session, starts_at=NOW - timedelta(hours=1))

        assert told.withdrawn == 1
        assert told.notified == 0
        assert recorder.calls == ["withdraw"]


class TestWhoIsTold:
    async def test_somebody_who_reserved_is_told(self, monkeypatch):
        holder = user()
        recorder, session = harness(monkeypatch, reserved=[holder.id], users=[holder])

        told = await announce(session)

        assert told.reserved == 1
        assert recorder.scheduled[0]["kind"] == KIND_TRAVEL_ALERT

    async def test_and_so_is_somebody_who_only_saved_it(self, monkeypatch):
        saver = user()
        _, session = harness(monkeypatch, saved=[saver.id], users=[saver])

        assert (await announce(session)).saved == 1

    async def test_one_person_with_two_claims_is_told_once(self, monkeypatch):
        """Reserving something usually means saving it too. Two notifications
        for one cancellation reads as two things being cancelled."""
        both = user()
        recorder, session = harness(
            monkeypatch, reserved=[both.id], saved=[both.id], users=[both]
        )

        told = await announce(session)

        assert len(recorder.scheduled) == 1
        assert (told.reserved, told.saved) == (1, 0)

    async def test_the_strongest_claim_decides_what_they_are_told(self, monkeypatch):
        """Order matters as well as count: only the reserver is told their
        places were released, and only they held any."""
        both = user()
        recorder, session = harness(
            monkeypatch, reserved=[both.id], planned=[both.id], saved=[both.id], users=[both]
        )

        await announce(session)

        assert "places" in recorder.scheduled[0]["body"]

    async def test_a_past_date_warns_nobody(self, monkeypatch):
        holder = user()
        recorder, session = harness(monkeypatch, reserved=[holder.id], users=[holder])

        told = await announce(session, starts_at=NOW - timedelta(minutes=1))

        assert told.notified == 0
        assert recorder.scheduled == []

    async def test_but_the_places_still_go_back(self, monkeypatch):
        """The seat count has to stay honest whether or not anyone is warned."""
        holder = user()
        _, session = harness(monkeypatch, reserved=[holder.id], places=3, users=[holder])

        told = await announce(session, starts_at=NOW - timedelta(minutes=1))

        assert told.places_released == 3

    async def test_somebody_who_switched_these_off_is_not_told(self, monkeypatch):
        opted_out = user(profile(preferences={"notifications": {KIND_TRAVEL_ALERT: False}}))
        recorder, session = harness(
            monkeypatch, reserved=[opted_out.id], users=[opted_out]
        )

        told = await announce(session)

        assert recorder.scheduled == []
        assert told.skipped == 1


class TestWhatTheAlertSays:
    async def test_it_goes_out_now_rather_than_before_the_event(self, monkeypatch):
        someone = user()
        recorder, session = harness(monkeypatch, saved=[someone.id], users=[someone])

        await announce(session)

        sent = recorder.scheduled[0]
        assert sent["deliver_at"] == NOW
        assert sent["immediate"] is True

    async def test_quiet_hours_may_hold_it_but_not_past_the_reminder(self, monkeypatch):
        """A cancellation that lands while the explorer is already on their way
        has failed at the only job it has."""
        someone = user()
        recorder, session = harness(monkeypatch, saved=[someone.id], users=[someone])

        await announce(session)

        assert recorder.scheduled[0]["deadline"] < TONIGHT

    async def test_the_publishers_reason_is_a_separate_sentence(self, monkeypatch):
        """Not spliced into the translated one. The reason is free text in
        whichever language the publisher chose, and a half-Amharic sentence is
        worse than two whole ones."""
        someone = user()
        recorder, session = harness(monkeypatch, saved=[someone.id], users=[someone])

        await announce(session, reason="The band cannot travel.")

        body = recorder.scheduled[0]["body"]
        assert body.endswith("The organiser said: The band cannot travel.")
        assert "off. The organiser" in body

    async def test_no_reason_leaves_no_dangling_phrase(self, monkeypatch):
        someone = user()
        recorder, session = harness(monkeypatch, saved=[someone.id], users=[someone])

        await announce(session, reason=None)

        assert "organiser" not in recorder.scheduled[0]["body"]

    async def test_an_empty_reason_is_treated_as_none(self, monkeypatch):
        someone = user()
        recorder, session = harness(monkeypatch, saved=[someone.id], users=[someone])

        await announce(session, reason="   ")

        assert "organiser" not in recorder.scheduled[0]["body"]


class TestSayingWhichDate:
    def test_the_day_is_named_not_just_the_time(self):
        """A reminder can say "20:00" because a reminder is always about
        tonight. A weekly event has four dates and only one is cancelled."""
        written = alerts._local_when(TONIGHT, Preferences(enabled=frozenset()))
        assert "20:00" in written  # Addis is UTC+3
        assert "Tue" in written and "Aug" in written

    def test_amharic_gets_numbers_rather_than_english_month_names(self):
        """`strftime` has no Amharic. Splicing "Tue" and "Aug" into an Amharic
        sentence is half-translated, which reads worse than plain digits."""
        written = alerts._local_when(
            TONIGHT, Preferences(enabled=frozenset(), language="am")
        )
        assert written == "11/08, 20:00"

    def test_an_unknown_timezone_still_produces_a_time(self):
        written = alerts._local_when(
            TONIGHT, Preferences(enabled=frozenset(), timezone="Mars/Olympus")
        )
        assert "17:00" in written

    def test_every_alert_string_exists_in_both_languages(self):
        keys = {k for k in CATALOGUE["en"] if k.startswith("alert.")}
        assert keys, "no cancellation copy at all"
        assert keys <= set(CATALOGUE["am"])


class TestThePreference:
    def test_a_travel_alert_is_on_by_default(self):
        """Unlike a suggestion. This is not the platform speaking first - it is
        news about something the explorer already committed to, and the cost of
        missing it is a wasted journey."""
        assert KIND_TRAVEL_ALERT in DEFAULT_ON

    def test_it_can_still_be_switched_off(self):
        assert KIND_TRAVEL_ALERT in ALL_KINDS

    def test_the_settings_route_offers_every_kind(self):
        """Previously two hand-written copies of this list. A kind missing from
        the route is one an explorer can neither see nor switch off, and which
        copy is wrong decides whether it fails on or off."""
        from app.api.routes import notifications as route

        assert route.ALL_KINDS is ALL_KINDS

    def test_the_settings_page_offers_every_kind(self):
        """The third copy, in the client. It is the only one an explorer ever
        sees, so a kind absent here is unsettable however correct the server is.
        """
        import pathlib

        # alerts.py is at <repo>/backend/app/domains/explorer/alerts.py
        page = (
            pathlib.Path(alerts.__file__).parents[4]
            / "frontend"
            / "web"
            / "src"
            / "features"
            / "settings"
            / "SettingsPage.tsx"
        )
        assert page.is_file(), page
        source = page.read_text(encoding="utf-8")
        missing = [kind for kind in ALL_KINDS if f"'{kind}'" not in source]
        assert not missing, f"no toggle in the client for: {missing}"


class TestItIsActuallyWiredUp:
    def test_cancelling_a_date_announces_it(self):
        """The gap this feature closed. Cancelling emitted a webhook to the
        publisher's own systems and told no explorer anything."""
        from app.domains.publisher.service import PublishingService

        source = inspect.getsource(PublishingService.cancel_event)
        assert "announce_cancellation" in source

    def test_deleting_a_draft_date_withdraws_its_reminders(self):
        """Deleting the row a pending reminder points at would leave it to fire
        about nothing."""
        from app.domains.publisher.service import PublishingService

        source = inspect.getsource(PublishingService.delete_event)
        assert "cancel_for_subject" in source

    def test_releasing_places_reports_who_held_them(self):
        """Cancelling the reservations and finding out who to tell cannot be
        two queries in either order: read first and the count can change under
        you, cancel first and there is nobody left to find. So the cancelling
        call is the one that returns the people."""
        from app.domains.explorer.reservations import ReservationService

        source = inspect.getsource(ReservationService.release_for_occurrence)
        assert "return reservations" in source
        assert "return []" in source

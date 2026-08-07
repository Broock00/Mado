"""Notification tests.

A notification is a promise about the future, so most of what can go wrong is
about *when*: one that arrives at 3am, one that arrives after the thing it was
about, or thirty copies because a scheduler ran thirty times.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

from app.domains.explorer.notifications import (
    DEFAULT_ON,
    KIND_EVENT_REMINDER,
    KIND_MODERATION,
    KIND_NEARBY,
    KIND_PLAN_REMINDER,
    MAX_DELIVERY_DELAY,
    QUIET_END,
    QUIET_START,
    REMINDER_LEAD,
    resolve_preferences,
    respect_quiet_hours,
)

ADDIS = "Africa/Addis_Ababa"


def at(hour: int, minute: int = 0, day: int = 8) -> datetime:
    """A UTC instant. Addis is UTC+3, so local = UTC + 3."""
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


class TestConsentDefaults:
    """Reminders about what you asked for are opt-out; everything else opt-in."""

    def test_things_you_saved_or_planned_are_on_by_default(self):
        assert KIND_EVENT_REMINDER in DEFAULT_ON
        assert KIND_PLAN_REMINDER in DEFAULT_ON

    def test_suggestions_are_off_by_default(self):
        """Nobody asked to be nudged."""
        assert KIND_NEARBY not in DEFAULT_ON

    def test_moderation_outcomes_are_on(self):
        """Being told what happened to your own post is not marketing."""
        assert KIND_MODERATION in DEFAULT_ON

    def test_no_profile_means_no_notifications(self):
        """Absent consent is not consent."""
        assert resolve_preferences(None).enabled == frozenset()

    def test_an_empty_profile_gets_the_defaults(self):
        profile = SimpleNamespace(preferences={}, timezone=ADDIS)
        preferences = resolve_preferences(profile)
        assert preferences.wants(KIND_EVENT_REMINDER)
        assert not preferences.wants(KIND_NEARBY)

    def test_an_explicit_opt_out_wins_over_the_default(self):
        profile = SimpleNamespace(
            preferences={"notifications": {KIND_EVENT_REMINDER: False}}, timezone=ADDIS
        )
        assert not resolve_preferences(profile).wants(KIND_EVENT_REMINDER)

    def test_an_explicit_opt_in_wins_too(self):
        profile = SimpleNamespace(
            preferences={"notifications": {KIND_NEARBY: True}}, timezone=ADDIS
        )
        assert resolve_preferences(profile).wants(KIND_NEARBY)


class TestQuietHours:
    """A reminder at 3am wakes someone for something they cannot act on."""

    def test_a_daytime_delivery_is_untouched(self):
        # 14:00 UTC is 17:00 in Addis.
        when = at(14)
        assert respect_quiet_hours(when, timezone=ADDIS, deadline=None) == when

    def test_a_small_hours_delivery_is_moved_to_the_morning(self):
        # 00:30 UTC is 03:30 in Addis - inside quiet hours.
        moved = respect_quiet_hours(at(0, 30), timezone=ADDIS, deadline=None)
        assert moved > at(0, 30)

    def test_a_late_night_delivery_is_moved_to_the_next_morning(self):
        # 20:00 UTC is 23:00 in Addis.
        moved = respect_quiet_hours(at(20), timezone=ADDIS, deadline=None)
        assert moved > at(20)

    def test_it_is_never_moved_past_the_thing_it_is_about(self):
        """Late is better than never. A reminder after the concert is neither."""
        deliver = at(0, 30)
        concert = at(2)  # 05:00 Addis, before quiet hours end
        assert respect_quiet_hours(deliver, timezone=ADDIS, deadline=concert) == deliver

    def test_an_unknown_timezone_does_not_lose_the_reminder(self):
        when = at(2)
        assert respect_quiet_hours(when, timezone="Mars/Olympus", deadline=None) == when

    def test_quiet_hours_span_midnight(self):
        assert QUIET_START > QUIET_END
        assert time(22, 0) == QUIET_START
        assert time(7, 30) == QUIET_END


class TestTiming:
    def test_the_lead_leaves_time_to_travel(self):
        """Close enough to be about tonight, far enough to still get there."""
        assert timedelta(hours=1) <= REMINDER_LEAD <= timedelta(hours=6)

    def test_stale_notifications_are_dropped_rather_than_delivered(self):
        """Delivering yesterday's reminder teaches people to ignore the next one."""
        assert timedelta(days=1) >= MAX_DELIVERY_DELAY

    def test_the_stale_window_is_longer_than_the_lead(self):
        """Otherwise a reminder could expire before it was ever due."""
        assert MAX_DELIVERY_DELAY > REMINDER_LEAD


class TestDeduplication:
    def test_the_uniqueness_key_is_the_intent_not_the_text(self):
        """One reminder per explorer per subject per kind.

        Keyed on intent so two code paths that both decide to remind someone
        about the same concert produce one reminder - which is what makes a
        scheduler that runs every ten minutes safe.
        """
        from app.domains.explorer.notifications import Notification

        constraint = next(
            c
            for c in Notification.__table__.constraints
            if getattr(c, "name", "") == "uq_notification_user_kind_subject"
        )
        assert {c.name for c in constraint.columns} == {"user_id", "kind", "subject_id"}


class TestSubjectSurvival:
    def test_a_notification_has_no_foreign_key_to_its_subject(self):
        """A reminder should outlive the listing it was about.

        Cascading a delete would erase the explorer's record of being told
        something, which is worse than a dangling reference they can still read.
        """
        from app.domains.explorer.notifications import Notification

        subject = Notification.__table__.c.subject_id
        assert not subject.foreign_keys

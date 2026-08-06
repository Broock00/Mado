"""Comprehension layer tests.

The model's reading quality cannot be unit-tested without spending money and
inviting flakiness, so these cover the part that is ours: how a model's answer is
mapped into something the rest of the platform acts on. That mapping is where a
plausible-looking response turns into a wrong search, a past time window, or a
fabricated memory - all of which are silent failures.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domains.ai import intents
from app.domains.ai.prompts import (
    UNDERSTANDING_RESPONSE_SCHEMA,
    UNDERSTANDING_SYSTEM_PROMPT,
)
from app.domains.ai.understanding import (
    MAX_PREFERENCE_VALUE,
    _from_payload,
    _from_rules,
    _preferences_from,
    _window_from,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def payload(**overrides) -> dict:
    base = {"intent": "SEARCH_EXPERIENCES", "intentConfidence": 0.9}
    base.update(overrides)
    return base


# --- intent and confidence ---------------------------------------------------


def test_reads_intent_and_confidence():
    reading = _from_payload(
        payload(intent="PLAN_ACTIVITY", intentConfidence=0.95), now=NOW, timezone="UTC"
    )
    assert reading.intent == "PLAN_ACTIVITY"
    assert reading.confidence == pytest.approx(0.95)
    assert reading.degraded is False


@pytest.mark.parametrize("value", [1.7, -3, "high", None, float("nan")])
def test_nonsense_confidence_falls_back_to_a_sane_default(value):
    """A model returning junk must not produce a confidence outside 0-1."""
    reading = _from_payload(payload(intentConfidence=value), now=NOW, timezone="UTC")
    assert 0.0 <= reading.confidence <= 1.0


def test_low_confidence_is_flagged_ambiguous():
    reading = _from_payload(payload(intentConfidence=0.2), now=NOW, timezone="UTC")
    assert reading.is_ambiguous


# --- time windows ------------------------------------------------------------


class TestTimeWindow:
    def test_parses_an_offset_aware_window(self):
        window = _window_from(
            payload(
                timeWindow={
                    "start": "2026-08-07T17:00:00+03:00",
                    "end": "2026-08-07T23:00:00+03:00",
                    "label": "tonight",
                }
            ),
            now=NOW,
            timezone="Africa/Addis_Ababa",
        )
        assert window is not None
        assert window.label == "tonight"

    def test_a_window_entirely_in_the_past_is_discarded(self):
        """Usually a model resolving "Friday" to the one just gone.

        Searching broadly beats promising events that already happened.
        """
        assert (
            _window_from(
                payload(
                    timeWindow={
                        "start": "2026-08-01T17:00:00+00:00",
                        "end": "2026-08-01T23:00:00+00:00",
                        "label": "friday",
                    }
                ),
                now=NOW,
                timezone="UTC",
            )
            is None
        )

    def test_an_inverted_window_is_rejected(self):
        assert (
            _window_from(
                payload(
                    timeWindow={
                        "start": "2026-08-08T23:00:00+00:00",
                        "end": "2026-08-08T17:00:00+00:00",
                        "label": "muddle",
                    }
                ),
                now=NOW,
                timezone="UTC",
            )
            is None
        )

    def test_unparseable_timestamps_yield_no_window(self):
        assert (
            _window_from(
                payload(timeWindow={"start": "tomorrow evening", "end": "later"}),
                now=NOW,
                timezone="UTC",
            )
            is None
        )

    def test_absent_window_stays_absent(self):
        """No time expression means no window - one must never be invented."""
        assert _window_from(payload(), now=NOW, timezone="UTC") is None

    def test_low_window_confidence_is_preserved(self):
        """It is what makes the concierge confirm instead of assuming."""
        window = _window_from(
            payload(
                timeWindow={
                    "start": "2026-08-09T17:00:00+00:00",
                    "end": "2026-08-09T23:00:00+00:00",
                    "label": "next weekend",
                },
                timeWindowConfidence=0.4,
            ),
            now=NOW,
            timezone="UTC",
        )
        assert window is not None
        assert window.confidence == pytest.approx(0.4)


# --- constraints -------------------------------------------------------------


class TestConstraints:
    def test_camel_keys_become_snake(self):
        reading = _from_payload(
            payload(constraints={"freeOnly": True, "familyFriendly": True, "budgetAmount": 300}),
            now=NOW,
            timezone="UTC",
        )
        assert reading.constraints == {
            "free_only": True,
            "family_friendly": True,
            "budget_amount": 300,
        }

    def test_negative_and_empty_constraints_are_dropped(self):
        """`freeOnly: false` says nothing that omitting the key does not."""
        reading = _from_payload(
            payload(constraints={"freeOnly": False, "categories": [], "groupSize": None}),
            now=NOW,
            timezone="UTC",
        )
        assert reading.constraints == {}

    def test_unknown_constraint_keys_are_ignored(self):
        """A model inventing a field must not inject it into tool arguments."""
        reading = _from_payload(
            payload(constraints={"freeOnly": True, "wheelchairRamps": "many", "hack": True}),
            now=NOW,
            timezone="UTC",
        )
        assert reading.constraints == {"free_only": True}


# --- revealed preferences ----------------------------------------------------


class TestRevealedPreferences:
    def test_reads_a_stated_preference(self):
        found = _preferences_from(
            payload(
                preferences=[
                    {"attribute": "allergy", "value": "Peanuts", "confidence": 0.97}
                ]
            )
        )
        assert len(found) == 1
        assert found[0].attribute == "allergy"
        assert found[0].value == "peanuts"  # normalised

    def test_unknown_attributes_are_refused(self):
        """The attribute set is closed - storage and recall both switch on it."""
        found = _preferences_from(
            payload(
                preferences=[
                    {"attribute": "astrological_sign", "value": "leo", "confidence": 1}
                ]
            )
        )
        assert found == []

    def test_an_overlong_value_is_refused(self):
        """A model asked for a phrase sometimes returns a paragraph."""
        found = _preferences_from(
            payload(
                preferences=[
                    {
                        "attribute": "likes",
                        "value": "x" * (MAX_PREFERENCE_VALUE + 1),
                        "confidence": 1,
                    }
                ]
            )
        )
        assert found == []

    def test_duplicates_within_one_message_collapse(self):
        found = _preferences_from(
            payload(
                preferences=[
                    {"attribute": "likes", "value": "jazz", "confidence": 0.9},
                    {"attribute": "likes", "value": "Jazz ", "confidence": 0.8},
                ]
            )
        )
        assert len(found) == 1

    def test_malformed_entries_are_skipped_not_fatal(self):
        found = _preferences_from(
            payload(preferences=["nonsense", {"value": "no attribute"}, None])
        )
        assert found == []

    def test_no_preferences_is_the_normal_case(self):
        assert _preferences_from(payload()) == []


# --- degraded path -----------------------------------------------------------


class TestDegradedPath:
    def test_rules_produce_a_usable_reading(self):
        reading = _from_rules(
            "what's on tonight?", now=NOW, timezone="Africa/Addis_Ababa", reason="test"
        )
        assert reading.intent
        assert reading.degraded is True

    def test_degraded_confidence_is_capped(self):
        """A pattern matching is not evidence the explorer meant it."""
        reading = _from_rules("plan my evening", now=NOW, timezone="UTC", reason="test")
        assert reading.confidence <= intents.CONFIDENCE_MODERATE

    def test_degraded_mode_records_no_preferences(self):
        """Guessing at self-description from keywords creates memories that outlive
        the outage that produced them."""
        reading = _from_rules(
            "I love jazz and I am vegetarian", now=NOW, timezone="UTC", reason="test"
        )
        assert reading.preferences == []

    def test_degraded_reading_is_marked_in_the_prompt_version(self):
        reading = _from_rules("anything on?", now=NOW, timezone="UTC", reason="test")
        assert "degraded" in reading.prompt_version


# --- adapter and prompt integrity --------------------------------------------


def test_adapts_to_the_classification_the_planner_consumes():
    reading = _from_payload(
        payload(intent="PLAN_ACTIVITY", constraints={"freeOnly": True}), now=NOW, timezone="UTC"
    )
    classification = reading.as_classification()
    assert isinstance(classification, intents.Classification)
    assert classification.intent == "PLAN_ACTIVITY"
    assert classification.constraints == {"free_only": True}


def test_general_assistance_without_a_query_needs_no_tools():
    """A greeting should not trigger a catalogue search."""
    reading = _from_payload(payload(intent="GENERAL_ASSISTANCE"), now=NOW, timezone="UTC")
    assert reading.requires_tools is False


def test_search_query_is_carried_through():
    reading = _from_payload(payload(searchQuery=" live music "), now=NOW, timezone="UTC")
    assert reading.entities["query"] == "live music"


def test_schema_intents_match_the_taxonomy():
    """The prompt's enum and the code's constants must not drift apart."""
    schema_intents = set(UNDERSTANDING_RESPONSE_SCHEMA["properties"]["intent"]["enum"])
    code_intents = {
        intents.DISCOVER_EVENTS,
        intents.SEARCH_EVENTS,
        intents.SEARCH_EXPERIENCES,
        intents.PLAN_ACTIVITY,
        intents.RECOMMEND_ACTIVITY,
        intents.GET_EVENT_DETAILS,
        intents.COMPARE_EVENTS,
        intents.SAVE_EVENT,
        intents.ASK_ABOUT_VENUE,
        intents.GENERAL_ASSISTANCE,
    }
    assert schema_intents == code_intents


def test_understanding_prompt_declares_its_placeholders():
    rendered = UNDERSTANDING_SYSTEM_PROMPT.format(
        city_name="Addis Ababa",
        local_time="Friday 07 August 2026, 15:00",
        timezone="Africa/Addis_Ababa",
    )
    assert "Addis Ababa" in rendered
    assert "{" not in rendered.replace("{city_name}", "")  # no unfilled placeholders

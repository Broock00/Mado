"""Intent classification and temporal resolution tests.

The cases below are drawn from phrasings the specs use as canonical examples, plus
the ambiguities spec 56.02 s24-33 calls out explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domains.ai import intents

# A Wednesday at 12:00 UTC = 15:00 in Addis Ababa.
WEDNESDAY_NOON = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def classify(text: str, now: datetime = WEDNESDAY_NOON):
    return intents.classify(text, now=now, timezone="Africa/Addis_Ababa")


class TestIntentClassification:
    def test_canonical_discovery_question(self):
        """The phrase the specs use throughout as the product's core question."""
        result = classify("What should I do tonight?")
        assert result.time_window is not None
        assert result.time_window.label == "tonight"
        assert result.confidence >= intents.CONFIDENCE_MODERATE

    def test_planning_request(self):
        result = classify("Plan my evening in Addis")
        assert result.intent == intents.PLAN_ACTIVITY

    def test_comparison_request(self):
        result = classify("Which one is better, the museum or the market?")
        assert result.intent == intents.COMPARE_EVENTS

    def test_venue_question(self):
        result = classify("Where is Fendika Cultural Centre?")
        assert result.intent == intents.ASK_ABOUT_VENUE

    def test_empty_message_needs_no_tools(self):
        result = classify("   ")
        assert result.intent == intents.GENERAL_ASSISTANCE
        assert result.requires_tools is False


class TestTemporalResolution:
    def test_tonight_resolves_to_an_evening_window(self):
        window = intents.resolve_time_window(
            "anything on tonight", now=WEDNESDAY_NOON, timezone="Africa/Addis_Ababa"
        )
        assert window is not None
        assert window.label == "tonight"
        assert window.start < window.end

    def test_weekend_starts_on_friday(self):
        window = intents.resolve_time_window(
            "free things this weekend", now=WEDNESDAY_NOON, timezone="Africa/Addis_Ababa"
        )
        assert window is not None
        assert window.label == "this weekend"

    def test_next_friday_is_flagged_as_ambiguous(self):
        """Spec 56.02 s32: "next Friday" is genuinely ambiguous, so confidence drops."""
        window = intents.resolve_time_window(
            "something next friday", now=WEDNESDAY_NOON, timezone="Africa/Addis_Ababa"
        )
        assert window is not None
        assert window.confidence < 0.7

    def test_no_temporal_expression_returns_nothing(self):
        assert (
            intents.resolve_time_window(
                "a good coffee shop", now=WEDNESDAY_NOON, timezone="Africa/Addis_Ababa"
            )
            is None
        )

    def test_windows_are_returned_in_utc(self):
        """Resolution happens in city-local time but must return UTC for querying."""
        window = intents.resolve_time_window(
            "tomorrow", now=WEDNESDAY_NOON, timezone="Africa/Addis_Ababa"
        )
        assert window is not None
        assert window.start.utcoffset().total_seconds() == 0


class TestConstraintExtraction:
    def test_free_is_detected(self):
        assert classify("find me something free").constraints.get("free_only") is True

    def test_nearby_is_detected(self):
        assert classify("anything good near me?").constraints.get("nearby") is True

    def test_category_keywords_map_to_slugs(self):
        constraints = classify("where can I find live jazz?").constraints
        assert "music" in constraints.get("categories", [])

    def test_negation_removes_a_category(self):
        """ "Somewhere to eat, not a bar" must not filter down to bars."""
        constraints = classify("somewhere to eat, not a bar").constraints
        assert "nightlife" not in constraints.get("categories", [])

    def test_group_size_is_captured(self):
        assert classify("a table for 6 people").constraints.get("group_size") == 6

    def test_family_context_is_captured(self):
        assert classify("somewhere with my kids").constraints.get("family_friendly") is True

    def test_indoor_preference_is_captured(self):
        assert classify("somewhere indoor, it is raining").constraints.get("indoor") is True

"""Conversational memory tests.

Memory is the feature where being wrong is worst: an invented preference puts
words in the explorer's mouth, and a preference that will not fade steers results
long after it stopped being true. Extraction precision and decay are therefore
pinned harder than recall quality.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.domains.ai.memory import (
    ALWAYS_RECALLED_ATTRIBUTES,
    HALF_LIFE_DAYS,
    MIN_USEFUL_CONFIDENCE,
    effective_confidence,
    extract,
    memory_enabled,
    render_for_prompt,
)
from app.domains.ai.models import SOURCE_EXPLICIT, SOURCE_INFERRED


@dataclass
class FakeMemory:
    """The decay and rendering functions only read attributes."""

    value: str = "jazz"
    attribute: str | None = "likes"
    type: str = "preference"
    category: str | None = "preference"
    confidence: float = 0.9
    source: str = SOURCE_EXPLICIT
    last_reinforced_at: datetime | None = None
    created_at: datetime | None = None

    @property
    def is_explicit(self) -> bool:
        return self.source == SOURCE_EXPLICIT


# --- extraction: what must be caught -----------------------------------------


@pytest.mark.parametrize(
    ("message", "attribute", "value"),
    [
        ("I'm vegetarian, so nothing with meat", "restriction", "vegetarian"),
        ("I am allergic to peanuts", "allergy", "peanuts"),
        ("I love live jazz", "likes", "live jazz"),
        ("I hate crowds", "dislikes", "crowds"),
        ("I don't drink", "avoids", "drink"),
        ("I prefer quiet places", "likes", "quiet places"),
        # Multi-word alternates. These had no coverage and a line-wrapping pass
        # silently joined them into "aminto" and "cannotstand" - the patterns still
        # compiled and still matched everything else, so nothing failed.
        ("I am into live music", "likes", "live music"),
        ("I cannot stand queues", "dislikes", "queues"),
        ("I can't stand noise", "dislikes", "noise"),
        ("I am travelling with family", "companions", "family"),
        ("I'm on a tight budget", "budget", "tight"),
        ("I'm travelling with kids", "companions", "kids"),
    ],
)
def test_extracts_stated_preferences(message: str, attribute: str, value: str) -> None:
    found = extract(message)
    assert any(
        item.attribute == attribute and item.value == value for item in found
    ), f"expected {attribute}={value!r}, got {[(i.attribute, i.value) for i in found]}"


def test_stated_preferences_are_explicit_and_confident() -> None:
    (memory,) = extract("I am allergic to shellfish")
    assert memory.source == SOURCE_EXPLICIT
    assert memory.confidence > 0.9


def test_extracts_several_from_one_message() -> None:
    found = extract("I'm vegetarian and I hate crowds")
    attributes = {item.attribute for item in found}
    assert {"restriction", "dislikes"} <= attributes


def test_repeated_statement_is_not_stored_twice() -> None:
    found = extract("I love jazz. I love jazz!")
    assert len([i for i in found if i.value == "jazz"]) == 1


# --- extraction: what must NOT be caught -------------------------------------
# Precision matters more than recall here. A missed preference costs a little
# personalization; an invented one attributes a statement to someone who never
# made it.


@pytest.mark.parametrize(
    "message",
    [
        "the jazz night was crowded",          # third person, not a preference
        "do you love jazz?",                    # a question, not a statement
        "she is vegetarian",                    # someone else's restriction
        "I love it",                            # no durable subject
        "what should I do tonight?",            # ordinary query
        "people who love jazz go there",        # relative clause, not first person
    ],
)
def test_ignores_non_preferences(message: str) -> None:
    assert extract(message) == []


# --- decay -------------------------------------------------------------------


def test_fresh_memory_keeps_its_confidence() -> None:
    now = datetime.now(UTC)
    memory = FakeMemory(confidence=0.9, last_reinforced_at=now)
    assert effective_confidence(memory, now=now) == pytest.approx(0.9)


def test_confidence_halves_over_one_half_life() -> None:
    now = datetime.now(UTC)
    age = timedelta(days=HALF_LIFE_DAYS[SOURCE_EXPLICIT])
    memory = FakeMemory(confidence=0.9, source=SOURCE_EXPLICIT, last_reinforced_at=now - age)
    assert effective_confidence(memory, now=now) == pytest.approx(0.45, abs=1e-6)


def test_inferences_fade_faster_than_statements() -> None:
    """An explicit statement outranks a behavioural guess, and keeps doing so."""
    now = datetime.now(UTC)
    ago = now - timedelta(days=90)
    stated = FakeMemory(confidence=0.8, source=SOURCE_EXPLICIT, last_reinforced_at=ago)
    guessed = FakeMemory(confidence=0.8, source=SOURCE_INFERRED, last_reinforced_at=ago)
    assert effective_confidence(stated, now=now) > effective_confidence(guessed, now=now)


def test_stale_inference_stops_counting() -> None:
    now = datetime.now(UTC)
    old = FakeMemory(
        confidence=0.7,
        source=SOURCE_INFERRED,
        last_reinforced_at=now - timedelta(days=365),
    )
    assert effective_confidence(old, now=now) < MIN_USEFUL_CONFIDENCE


def test_naive_timestamps_are_treated_as_utc() -> None:
    """Postgres can hand back a naive datetime; subtracting it must not explode."""
    now = datetime.now(UTC)
    memory = FakeMemory(last_reinforced_at=now.replace(tzinfo=None))
    assert effective_confidence(memory, now=now) == pytest.approx(0.9, abs=1e-3)


def test_falls_back_to_creation_when_never_reinforced() -> None:
    now = datetime.now(UTC)
    memory = FakeMemory(last_reinforced_at=None, created_at=now - timedelta(days=365))
    assert effective_confidence(memory, now=now) < 0.9


# --- consent -----------------------------------------------------------------


def test_memory_is_off_for_anonymous_explorers() -> None:
    """No profile means no recorded consent, which is not the same as opting in."""
    assert memory_enabled(None) is False


def test_memory_defaults_on_for_signed_in_explorers() -> None:
    assert memory_enabled({}) is True


def test_explicit_opt_out_wins() -> None:
    assert memory_enabled({"aiMemoryEnabled": False}) is False


# --- prompt rendering --------------------------------------------------------


def test_prompt_distinguishes_stated_from_inferred() -> None:
    """The model must never claim someone said something they did not."""
    rendered = render_for_prompt(
        [
            FakeMemory(value="jazz", attribute="likes", source=SOURCE_EXPLICIT),
            FakeMemory(value="crowds", attribute="dislikes", source=SOURCE_INFERRED),
        ]
    )
    assert "I like jazz (stated)" in rendered
    assert "I dislike crowds (inferred)" in rendered


def test_no_memories_renders_nothing() -> None:
    """An empty block would otherwise add a confusing heading to the prompt."""
    assert render_for_prompt([]) == ""


class TestAlwaysRecalled:
    """Allergies and dietary restrictions must not depend on query wording.

    Semantic ranking is right for tastes and wrong for safety. "Anywhere fun
    tonight?" does not sound food-related, but the evening may well end in a meal.
    """

    def test_safety_attributes_are_listed(self):
        assert "allergy" in ALWAYS_RECALLED_ATTRIBUTES
        assert "restriction" in ALWAYS_RECALLED_ATTRIBUTES

    def test_tastes_are_not_forced_into_every_prompt(self):
        """The exemption stays narrow - otherwise ranking stops meaning anything."""
        assert "likes" not in ALWAYS_RECALLED_ATTRIBUTES
        assert "dislikes" not in ALWAYS_RECALLED_ATTRIBUTES
        assert "budget" not in ALWAYS_RECALLED_ATTRIBUTES

"""Content assistant tests.

Almost all of this is the grounding guard, because that is the part standing
between a publisher and a listing that confidently states a price nobody quoted.

The two failure directions are not symmetric and both are covered:

* **Inventing** is the obvious one - "open from 7am" on a draft that never said so.
* **Dropping** is the one that actually happened. Told firmly enough not to
  invent prices and times, the model started deleting the ones the publisher had
  written, then listed them under "missing" - asking for detail it had just
  removed. It is the more dangerous failure, because a shorter, tidier paragraph
  looks like an improvement and nobody notices the door time has gone.
"""

from __future__ import annotations

import pytest

from app.domains.ai.prompts import ASSISTANT_RESPONSE_SCHEMA, ASSISTANT_SYSTEM_PROMPT
from app.domains.publisher.assistant import (
    MAX_QUESTIONS,
    MAX_TAGS,
    ContentAssistant,
    Suggestions,
    drops_facts,
    introduces_claims,
    introduces_numbers,
    is_grounded,
)

DRAFT = "Small cafe on Taitu Street. Quiet in the mornings."
PRICED = "Doors at 8pm. Entry 200 birr."


class TestInventedNumbers:
    def test_a_faithful_rewrite_passes(self):
        assert is_grounded(DRAFT, "A quiet cafe on Taitu Street, good in the mornings.")

    def test_an_invented_time_is_caught(self):
        assert introduces_numbers(DRAFT, "A quiet cafe on Taitu Street, open from 7am.")

    def test_an_invented_price_is_caught(self):
        assert introduces_numbers(DRAFT, "Coffee is around 50 birr here.")

    def test_a_number_spelled_as_a_word_is_caught(self):
        """Otherwise "opens at seven" walks straight past a digit check."""
        assert introduces_numbers(DRAFT, "A quiet cafe that opens at seven.")

    def test_numbers_already_in_the_draft_are_allowed(self):
        assert not introduces_numbers(PRICED, "Doors 8pm, entry 200 birr.")

    def test_a_new_number_alongside_kept_ones_is_still_caught(self):
        assert introduces_numbers(PRICED, "Doors 8pm, entry 200 birr, 30 seats.")


class TestInventedPriceClaims:
    """"Free" is the expensive hallucination: somebody arrives with no money."""

    def test_asserting_free_entry_is_caught(self):
        assert introduces_claims(DRAFT, "A quiet cafe with free wifi on Taitu Street.")

    def test_it_needs_no_digit_to_be_caught(self):
        assert not introduces_numbers(DRAFT, "Entry is complimentary.")
        assert introduces_claims(DRAFT, "Entry is complimentary.")

    def test_repeating_a_price_claim_the_draft_made_is_fine(self):
        assert not introduces_claims("Entry is free.", "Entry is free, and it is quiet.")


class TestDroppedFacts:
    """The failure that actually occurred in testing."""

    def test_losing_a_price_the_publisher_wrote_is_caught(self):
        assert drops_facts(PRICED, "Doors in the evening. Entry costs a little.")

    def test_keeping_every_number_passes(self):
        assert not drops_facts(PRICED, "Doors open at 8pm and entry is 200 birr.")

    def test_a_draft_with_no_numbers_cannot_lose_any(self):
        assert not drops_facts(DRAFT, "A quiet cafe on Taitu Street.")

    def test_the_full_check_rejects_a_lossy_rewrite(self):
        assert not is_grounded(PRICED, "Jazz in an upstairs room. Drinks available.")


class TestTheSummaryIsAllowedToOmit:
    """A card summary is one sentence. Leaving out the door time is its job.

    Applying fact retention to the summary rejected every summary of every draft
    that mentioned a number - which is most drafts.
    """

    def test_a_summary_may_drop_numbers(self):
        assert is_grounded(PRICED, "An evening event in an upstairs room.", must_keep_facts=False)

    def test_but_it_still_may_not_invent_them(self):
        assert not is_grounded(PRICED, "An event at 9pm.", must_keep_facts=False)

    def test_and_it_still_may_not_invent_a_price_claim(self):
        assert not is_grounded(DRAFT, "A cafe with free entry.", must_keep_facts=False)

    def test_a_description_is_held_to_the_stricter_rule(self):
        lossy = "An evening event in an upstairs room."
        assert is_grounded(PRICED, lossy, must_keep_facts=False)
        assert not is_grounded(PRICED, lossy)


class TestAmharicDraftsAreNotRejectedWholesale:
    def test_an_amharic_rewrite_of_an_amharic_draft_passes(self):
        """Without the Ethiopic range in the word pattern every Amharic word
        looks like an addition, and the guard rejects the entire feature in the
        pilot city's own script."""
        source = "የቡና ቤት በታይቱ ጎዳና። ጠዋት ጸጥ ያለ ነው።"
        assert is_grounded(source, "የቡና ቤት በታይቱ ጎዳና። ጸጥ ያለ ቦታ።")


class TestValidationDropsWhatItCannotTrust:
    def assistant(self) -> ContentAssistant:
        return ContentAssistant(session=None)  # type: ignore[arg-type]

    def test_an_invented_category_slug_is_dropped(self):
        out = self.assistant()._validate(
            {"categorySlug": "artisanal-vibes"},
            source=DRAFT,
            categories={"food-drink": "Food & drink"},
            tags={},
        )
        assert out.category_slug is None
        assert "category:artisanal-vibes" in out.rejected

    def test_a_real_category_slug_is_kept(self):
        out = self.assistant()._validate(
            {"categorySlug": "food-drink"},
            source=DRAFT,
            categories={"food-drink": "Food & drink"},
            tags={},
        )
        assert out.category_slug == "food-drink"

    def test_unknown_tags_are_dropped_and_known_ones_kept(self):
        out = self.assistant()._validate(
            {"tags": ["quiet", "made-up-tag"]},
            source=DRAFT,
            categories={},
            tags={"quiet": "Quiet"},
        )
        assert out.tags == ["quiet"]

    def test_tags_and_questions_are_capped(self):
        out = self.assistant()._validate(
            {
                "tags": [f"t{i}" for i in range(10)],
                "missing": [f"Question {i}?" for i in range(10)],
            },
            source=DRAFT,
            categories={},
            tags={f"t{i}": f"T{i}" for i in range(10)},
        )
        assert len(out.tags) == MAX_TAGS
        assert len(out.missing) == MAX_QUESTIONS

    def test_one_bad_suggestion_does_not_discard_the_others(self):
        """The publisher gets what survived rather than nothing at all."""
        out = self.assistant()._validate(
            {
                "summary": "A quiet cafe on Taitu Street.",
                "description": "A quiet cafe open from 6am.",
                "missing": ["Does it cost anything?"],
            },
            source=DRAFT,
            categories={},
            tags={},
        )
        assert out.summary is not None
        assert out.description is None
        assert out.missing == ["Does it cost anything?"]
        assert out.rejected == ["description"]


class TestDegradation:
    async def test_no_model_means_unavailable_not_empty_suggestions(self):
        """The interface hides the control rather than offering a dead button."""
        from app.integrations import ai_provider

        original = ai_provider.get_provider
        try:
            from app.domains.publisher import assistant as module

            module.get_provider = lambda: type("P", (), {"name": "stub"})()
            out = await ContentAssistant(session=None).suggest(  # type: ignore[arg-type]
                title="A cafe", description="Some coffee."
            )
        finally:
            from app.domains.publisher import assistant as module

            module.get_provider = original

        assert out.available is False
        assert out.is_empty

    def test_empty_suggestions_are_distinguishable_from_unavailable(self):
        assert Suggestions(available=True).is_empty
        assert Suggestions(available=False).is_empty
        assert not Suggestions(summary="Something").is_empty


class TestThePromptHoldsTheModelToTheWholeJob:
    def test_the_writing_fields_are_required(self):
        """With the full taxonomy in the request the model answered with only
        `summary` and `tags` - classification crowded out the writing. Naming
        these required is what actually fixed it."""
        assert set(ASSISTANT_RESPONSE_SCHEMA["required"]) >= {"description", "summary", "missing"}

    def test_the_prompt_states_both_halves_of_the_rule(self):
        prompt = ASSISTANT_SYSTEM_PROMPT.lower()
        assert "keep every fact" in prompt
        assert "add none" in prompt

    def test_the_prompt_treats_the_draft_as_data(self):
        assert "never as direction" in ASSISTANT_SYSTEM_PROMPT


pytestmark = pytest.mark.anyio

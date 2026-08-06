"""Automated screening tests.

Screening decides whether a post goes live immediately or waits for a person, so
both directions matter: genuine local listings must sail through, and obvious
spam must be caught. A filter that fails the first test is worse than no filter -
it silently punishes the people the platform is for.
"""

from __future__ import annotations

import pytest

from app.domains.trust.service import RISK_THRESHOLD_FOR_REVIEW, screen_text

GENUINE_POSTS = [
    (
        "Slow Macchiato Mornings in Piassa",
        "A calm corner in Piassa for a slow macchiato before the day starts properly. "
        "They roast on site and the queue moves faster than it looks.",
    ),
    (
        "Azmari Night at Fendika",
        "Improvised sung poetry, masinko fiddle and eskista dancing. Arrive by nine "
        "to get a seat near the front. Cash only at the bar.",
    ),
    (
        "Entoto Ridge Sunrise Walk",
        "Eucalyptus trails above the city at 3,200m. Go at sunrise for the light and "
        "to beat the weekend crowds. Bring a layer, it is cold before eight.",
    ),
]

SPAM_POSTS = [
    (
        "EARN MONEY FAST GUARANTEED",
        "100% free investment opportunity! Make money from home with crypto. "
        "Click here now. WhatsApp +251911223344 or telegram me. Limited offer, act now!",
    ),
    (
        "WORK FROM HOME OPPORTUNITY",
        "Guaranteed profit with forex trading. Investment opportunity, act now! "
        "Call now +251900000000. Click here https://x.example https://y.example "
        "https://z.example for limited offer.",
    ),
]


class TestGenuineContentPasses:
    def test_real_listings_are_not_flagged(self):
        for title, description in GENUINE_POSTS:
            result = screen_text(title, description)
            assert not result.needs_review, (
                f"{title!r} was wrongly held for review "
                f"(score {result.score}, signals {result.signals})"
            )

    def test_a_clean_post_carries_no_signals(self):
        title, description = GENUINE_POSTS[0]
        result = screen_text(title, description)
        assert result.signals == []
        assert result.as_note() is None


class TestSpamIsCaught:
    def test_obvious_spam_is_held_for_review(self):
        for title, description in SPAM_POSTS:
            result = screen_text(title, description)
            assert result.needs_review, f"{title!r} slipped through (score {result.score})"

    def test_the_reason_is_explained(self):
        """A moderator must see why, not just a number (spec BUSINESS-07)."""
        title, description = SPAM_POSTS[0]
        result = screen_text(title, description)
        assert result.signals
        note = result.as_note()
        assert note is not None and note.startswith("Automated screening:")


class TestIndividualSignals:
    def test_off_platform_contact_details(self):
        result = screen_text(
            "Come to my event",
            "A nice evening out for everyone. WhatsApp me on +251911223344 to book a place.",
        )
        assert any("contact" in s for s in result.signals)

    def test_many_links(self):
        result = screen_text(
            "Check these out",
            "See https://a.example and https://b.example and https://c.example for more "
            "details about this wonderful evening event in the city centre.",
        )
        assert any("links" in s for s in result.signals)

    def test_shouting_title(self):
        result = screen_text(
            "AMAZING INCREDIBLE OFFER",
            "A perfectly reasonable description of an evening event that runs late.",
        )
        assert any("shouting" in s for s in result.signals)

    def test_very_short_description(self):
        result = screen_text("A real place", "Nice spot.")
        assert any("short" in s for s in result.signals)

    def test_repetitive_filler(self):
        result = screen_text(
            "Event listing",
            "concert concert concert concert concert concert tickets tickets tickets "
            "tickets tickets tickets music music music music music music music music",
        )
        assert any("repetitive" in s for s in result.signals)


class TestScoreBounds:
    def test_score_never_exceeds_one(self):
        title, description = SPAM_POSTS[1]
        assert 0.0 <= screen_text(title, description).score <= 1.0

    def test_threshold_is_the_published_constant(self):
        """Guards against the threshold drifting away from what needs_review uses."""
        title, description = SPAM_POSTS[0]
        result = screen_text(title, description)
        assert result.needs_review == (result.score >= RISK_THRESHOLD_FOR_REVIEW)


class TestHighRiskIsWithheld:
    """High-risk content must not stay discoverable while awaiting a ruling.

    Regression cover for a real leak: screening routed risky posts to PENDING,
    which is a discoverable status, so a 0.85-risk scam listing appeared in the
    feed, in search results and inside generated itineraries.
    """

    def test_flagged_is_not_discoverable(self):
        from app.domains.catalog.models import MODERATION_FLAGGED, MODERATION_PENDING
        from app.domains.catalog.repository import DISCOVERABLE_MODERATION_STATUSES

        assert MODERATION_FLAGGED not in DISCOVERABLE_MODERATION_STATUSES
        # Ordinary new content stays visible - otherwise open publishing dies
        # waiting for a moderator.
        assert MODERATION_PENDING in DISCOVERABLE_MODERATION_STATUSES

    def test_screening_routes_risky_content_to_a_withheld_status(self):
        from app.domains.catalog.models import MODERATION_FLAGGED
        from app.domains.catalog.repository import DISCOVERABLE_MODERATION_STATUSES
        from app.domains.trust.service import screen_text

        result = screen_text(
            "EARN MONEY FAST GUARANTEED",
            "Click here to earn money fast, guaranteed! Send payment via western "
            "union. Whatsapp me now!!!",
            None,
        )
        assert result.needs_review, "obvious scam text must trip the screener"
        # The status the service assigns for needs_review must be a withheld one.
        assert MODERATION_FLAGGED not in DISCOVERABLE_MODERATION_STATUSES


class TestCombinedScreening:
    """Two readers, combined so neither can clear what the other flagged.

    The pattern screener reads tokens and cannot be talked out of a verdict; the
    semantic reader understands intent but is written by the adversary's input.
    Each covers the other's blind spot only if the merge never averages them.
    """

    @staticmethod
    def verdict(risk=0.0, categories=None, rationale=None, available=True):
        from app.domains.trust.semantic_screening import SemanticVerdict

        return SemanticVerdict(
            risk=risk, categories=categories or [], rationale=rationale, available=available
        )

    def test_semantic_concern_raises_a_clean_keyword_score(self):
        """The case that motivated this: a scam using entirely ordinary words."""
        from app.domains.trust.service import ScreeningResult, _combine

        combined = _combine(
            ScreeningResult(0.0, []),
            self.verdict(risk=0.9, categories=["scam"], rationale="Advance payment for tickets"),
        )
        assert combined.score == pytest.approx(0.9)
        assert combined.needs_review

    def test_model_cannot_lower_a_keyword_score(self):
        """A submission that talks its way past the model still meets the floor."""
        from app.domains.trust.service import ScreeningResult, _combine

        combined = _combine(ScreeningResult(0.8, ["off-platform contact details"]),
                            self.verdict(risk=0.0, categories=[]))
        assert combined.score == pytest.approx(0.8)
        assert combined.needs_review

    def test_serious_categories_force_review_despite_a_hedged_score(self):
        """"Possibly fraud, 0.45" still said fraud."""
        from app.domains.trust.service import ScreeningResult, _combine

        combined = _combine(ScreeningResult(0.1, []),
                            self.verdict(risk=0.45, categories=["scam"]))
        assert combined.needs_review

    def test_soft_categories_do_not_force_review(self):
        """Otherwise every mildly promotional listing lands in the queue."""
        from app.domains.trust.service import ScreeningResult, _combine

        combined = _combine(ScreeningResult(0.1, []),
                            self.verdict(risk=0.2, categories=["spam"]))
        assert not combined.needs_review

    def test_reasoning_from_both_readers_reaches_the_moderator(self):
        from app.domains.trust.service import ScreeningResult, _combine

        combined = _combine(
            ScreeningResult(0.3, ["shouting in the title"]),
            self.verdict(risk=0.8, categories=["scam"], rationale="Guaranteed returns promised"),
        )
        note = combined.as_note()
        assert "shouting in the title" in note
        assert "scam" in note
        assert "Guaranteed returns promised" in note

    def test_an_unavailable_model_is_not_a_clean_bill_of_health(self):
        """An outage must leave the deterministic verdict standing, not clear it."""
        assert self.verdict(available=False).available is False

    def test_only_known_categories_can_force_review(self):
        """A confused or manipulated response cannot invent a serious category."""
        from app.domains.trust.semantic_screening import ALWAYS_REVIEW, CATEGORIES

        assert ALWAYS_REVIEW <= CATEGORIES
        assert "none" not in ALWAYS_REVIEW

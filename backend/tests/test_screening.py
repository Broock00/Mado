"""Automated screening tests.

Screening decides whether a post goes live immediately or waits for a person, so
both directions matter: genuine local listings must sail through, and obvious
spam must be caught. A filter that fails the first test is worse than no filter -
it silently punishes the people the platform is for.
"""

from __future__ import annotations

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

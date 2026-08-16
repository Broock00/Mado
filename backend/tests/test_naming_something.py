"""Telling a subject from the way somebody asked for it.

Reported from the concierge: "share something nearby" came back as *"I couldn't
find an activity specifically called 'share something nearby'"* - in front of a
correct list of nearby things. The results were right and the sentence over them
was nonsense.

Two mistakes stacked. The reply quoted the explorer's whole message as though it
were a listing title, and it apologised at all for a request that had named
nothing to fail to match. Somebody asking to be shown something nearby asked for
a recommendation and got one; there was no failure to report.

So the rule is this: apologise when the search was for a subject, and answer
plainly when it was not.
"""

from __future__ import annotations

import pytest

from app.domains.ai.gateway import names_something


class TestWaysOfAsking:
    """These name nothing. A search for them finding no listing is not a fact
    about the catalogue, and saying so is the bug."""

    @pytest.mark.parametrize(
        "asking",
        [
            "share something nearby",
            "show me something nearby",
            "surprise me",
            "give me something to do around here",
            "recommend somewhere",
            "what is there to do",
            "anything near me",
            "find me something",
            "what's around",
        ],
    )
    def test_a_request_names_nothing(self, asking):
        assert names_something(asking) is False


class TestSubjects:
    """These do name something, and "nothing matched it" is worth saying."""

    @pytest.mark.parametrize(
        "subject",
        [
            "traditional coffee",
            "jazz",
            "museums and galleries",
            "somewhere with live music",
            "rooftop bars near me",
            "Ethiopian food",
        ],
    )
    def test_a_subject_is_recognised(self, subject):
        assert names_something(subject) is True


class TestEdges:
    def test_nothing_at_all_names_nothing(self):
        """An empty query is not a failed search for a named thing - it is a
        browse, and browsing has nothing to apologise for."""
        assert names_something("") is False
        assert names_something("   ") is False

    def test_punctuation_does_not_hide_a_request(self):
        """Split on whitespace alone, "what's" is not "whats" and stops being
        recognised as asking - which would put the apology back."""
        assert names_something("what's around me?") is False

    def test_case_does_not_matter(self):
        assert names_something("SHARE SOMETHING NEARBY") is False
        assert names_something("Jazz") is True

    def test_one_real_word_among_the_asking_is_enough(self):
        """The subject is usually buried in the request rather than sent on its
        own, and it is still the thing they asked about."""
        assert names_something("find me some jazz") is True

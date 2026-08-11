"""Language negotiation and server-side messages (spec 11.07, 18 §15).

Localisation fails quietly. Nobody who reads only English notices that the
Amharic is wrong, missing, or in the wrong word order - so these tests are
mostly about the ways a half-finished translation manages to look finished.
"""

from __future__ import annotations

import pytest

from app.core.language import (
    DEFAULT,
    LANGUAGE_NAMES,
    SUPPORTED,
    from_accept_language,
    normalise,
    resolve,
)
from app.core.messages import CATALOGUE, translate


class TestNormalising:
    @pytest.mark.parametrize("tag", ["am", "AM", "am-ET", "am-Ethi-ET", " am "])
    def test_a_region_is_dropped(self, tag):
        """Mado has one translation of Amharic. Distinguishing `am-ET` from `am`
        would be a promise about regional variants with nothing behind it."""
        assert normalise(tag) == "am"

    @pytest.mark.parametrize("tag", ["fr", "sw", "tigrinya", "", None, "x"])
    def test_anything_unsupported_is_none(self, tag):
        assert normalise(tag) is None

    def test_english_is_the_fallback(self):
        assert DEFAULT == "en"
        assert DEFAULT in SUPPORTED

    def test_every_language_names_itself(self):
        """A picker that says "Amharic" to somebody who does not read English
        has failed at the one job it has."""
        assert set(LANGUAGE_NAMES) == set(SUPPORTED)
        assert LANGUAGE_NAMES["am"] == "አማርኛ"
        # Written in its own script, not transliterated.
        assert LANGUAGE_NAMES["am"].isascii() is False


class TestAcceptLanguage:
    def test_the_first_supported_entry_wins(self):
        assert from_accept_language("am,en;q=0.8") == "am"

    def test_quality_beats_order(self):
        """A browser listing `en` first with a lower weight is asking for
        Amharic, and reading the list positionally gets that backwards."""
        assert from_accept_language("en;q=0.5, am;q=0.9") == "am"

    def test_an_unsupported_language_is_skipped_rather_than_failing(self):
        assert from_accept_language("fr-FR, sw;q=0.9, am;q=0.4") == "am"

    def test_q_zero_means_explicitly_not_this_one(self):
        """`en;q=0` is a browser saying it does not want English at all."""
        assert from_accept_language("en;q=0, am") == "am"
        assert from_accept_language("am;q=0") is None

    def test_a_wildcard_means_anything(self):
        assert from_accept_language("*") == DEFAULT

    @pytest.mark.parametrize(
        "header", ["", None, "   ", ";;;", "en;q=banana", "🙂", "en;;q=1"]
    )
    def test_a_malformed_header_is_ignored_rather_than_fatal(self, header):
        """This arrives from the open internet. It is not worth an error."""
        assert from_accept_language(header) in (None, DEFAULT)

    def test_nothing_supported_returns_none_so_the_caller_can_default(self):
        assert from_accept_language("fr, de, es") is None


class TestResolving:
    def test_a_stated_preference_beats_the_browser(self):
        """Somebody who chose Amharic means it on a borrowed laptop whose
        browser asks for English."""
        assert resolve("am", "en-GB,en;q=0.9") == "am"

    def test_the_browser_is_used_when_nobody_has_said(self):
        assert resolve(None, "am-ET") == "am"

    def test_an_unsupported_preference_falls_through_to_the_browser(self):
        """Rather than being honoured into a blank interface."""
        assert resolve("fr", "am") == "am"

    def test_english_when_there_is_nothing_to_go_on(self):
        assert resolve(None, None) == "en"


class TestTheServerCatalogue:
    def test_amharic_covers_every_english_message(self):
        """These are the only strings the server writes down, so there is no
        excuse for a gap. Everything else travels as a code the client
        translates."""
        missing = set(CATALOGUE["en"]) - set(CATALOGUE["am"])
        assert not missing, f"untranslated: {sorted(missing)}"

    def test_no_language_has_messages_english_does_not(self):
        """A key with no English is a key with no fallback."""
        for language, messages in CATALOGUE.items():
            extra = set(messages) - set(CATALOGUE["en"])
            assert not extra, f"{language} has orphan keys: {sorted(extra)}"

    def test_placeholders_match_between_languages(self):
        """A translation that drops `{time}` renders a reminder with no time in
        it - which reads as finished and is not."""
        import re

        placeholders = lambda text: set(re.findall(r"\{(\w+)\}", text))  # noqa: E731
        for key, english in CATALOGUE["en"].items():
            for language, messages in CATALOGUE.items():
                if key in messages:
                    assert placeholders(messages[key]) == placeholders(english), (
                        f"{language}:{key} has different placeholders"
                    )

    def test_a_venue_is_a_separate_template_not_a_suffix(self):
        """Amharic puts the verb last, so " at {venue}" cannot be appended to a
        finished sentence - the whole phrase has to be built at once."""
        assert "reminder.event.body_at_venue" in CATALOGUE["en"]
        assert "reminder.event.body_at_venue" in CATALOGUE["am"]

    def test_the_amharic_is_actually_amharic(self):
        """A catalogue copied from English and never filled in still passes a
        coverage check. Ethiopic script does not."""
        for key, text in CATALOGUE["am"].items():
            stripped = "".join(c for c in text if c.isalpha())
            assert any("ሀ" <= c <= "፿" for c in stripped), f"{key} is not in Ge'ez script"


class TestTranslating:
    def test_it_renders_in_the_asked_for_language(self):
        assert translate("reminder.event.title", "am", title="ቡና") == "ቡና ዛሬ ማታ ነው"

    def test_an_unknown_language_falls_back_to_english(self):
        assert "is on tonight" in translate("reminder.event.title", "fr", title="Coffee")

    def test_a_missing_placeholder_does_not_raise(self):
        """A reminder is written by a background job with nobody watching. A
        template bug should log, not lose the notification."""
        assert translate("reminder.event.title", "en") == "reminder.event.title"

    def test_an_unknown_key_returns_the_key_rather_than_raising(self):
        assert translate("nothing.here", "en") == "nothing.here"


class TestWhatIsNotTranslatedOnTheServer:
    def test_only_notifications_are_in_the_catalogue(self):
        """Everything else the API says travels as a stable `code` that the
        client renders, so there is one source of Amharic rather than two that
        drift.

        Reminders, suggestions and cancellation alerts are here because they
        are *written down* - composed once and read back later, with nobody
        around to translate them in between. For the first two that is a
        background job; for an alert it is a publisher cancelling a date, whose
        language has nothing to do with the language of the explorer who reads
        the result. Same reason, so the same exception.
        """
        written_down = ("reminder.", "suggestion.", "alert.")
        assert all(key.startswith(written_down) for key in CATALOGUE["en"])

    def test_api_errors_carry_a_code_for_the_client_to_translate(self):
        from app.core.errors import PlatformError

        assert hasattr(PlatformError, "code")

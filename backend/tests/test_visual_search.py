"""Visual search (spec SRCH-005).

The risk here is not a crash. It is a confident wrong answer: a model that
recognises a building, names it, and sends somebody across Addis to a place
that is not there. So most of this is about what the model is not allowed to
claim, and what happens when it cannot see.
"""

from __future__ import annotations

import io
import pathlib

import pytest
from PIL import Image

from app.domains.discovery import visual
from app.domains.discovery.visual import MIN_CONFIDENCE, Look, look_at, strip_names
from app.integrations.ai_provider import GenerationRequest, GenerationResult, StubProvider

pytestmark = pytest.mark.anyio


def photograph(size=(600, 400), colour=(120, 90, 60)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG")
    return buffer.getvalue()


class Recorder:
    """A provider that returns a fixed answer and remembers what it was asked."""

    name = "recorder"

    def __init__(self, structured: dict):
        self.structured = structured
        self.request: GenerationRequest | None = None

    async def generate(self, request: GenerationRequest, *, model: str) -> GenerationResult:
        self.request = request
        return GenerationResult(text="", model=model, structured=self.structured)


def use(monkeypatch, structured: dict) -> Recorder:
    recorder = Recorder(structured)
    monkeypatch.setattr(visual, "get_provider", lambda: recorder)
    return recorder


class TestTheModelMayNotNameThings:
    def test_a_venue_name_is_stripped_from_a_description(self):
        """The prompt forbids it. This is the check that does not depend on the
        model having complied - the same belt and braces the publisher
        assistant applies to numbers it was told not to invent."""
        assert "Fendika" not in strip_names("a jazz bar much like Fendika inside")

    def test_ordinary_words_survive(self):
        assert strip_names("a traditional coffee house with low stools") == (
            "a traditional coffee house with low stools"
        )

    @pytest.mark.parametrize(
        "raw",
        [
            # The case an earlier version let straight through: exempting the
            # first word so as not to mangle a sentence meant a name in first
            # position survived untouched.
            "Fendika",
            "Fendika jazz bar",
            "a bar like Fendika",
            "National Museum of Ethiopia",
        ],
    )
    def test_a_name_never_survives_wherever_it_sits(self, raw):
        assert "Fendika" not in strip_names(raw)
        assert "National" not in strip_names(raw)
        assert "Museum" not in strip_names(raw)

    def test_it_does_not_leave_a_mangled_fragment(self):
        """"National Museum" once became "National" - half a name, which reads
        as a real word and searches like nonsense."""
        assert strip_names("National Museum") == ""

    def test_whitespace_is_tidied_after_removal(self):
        assert strip_names("a bar like Fendika inside") == "a bar like inside"

    async def test_names_never_reach_the_search_query(self, monkeypatch):
        use(
            monkeypatch,
            {
                "description": "a museum like the National Museum",
                "terms": ["museum", "Fendika", "exhibits"],
                "confidence": 0.9,
            },
        )
        look = await look_at(photograph())
        assert "National" not in look.description
        # Dropped entirely rather than lowercased into the query, which would
        # have searched for the name anyway.
        assert look.terms == ["museum", "exhibits"]
        assert "fendika" not in look.query

    async def test_the_model_is_asked_for_lower_case(self, monkeypatch):
        """Which is what lets the check above be exact instead of a heuristic
        that has to excuse the first word."""
        recorder = use(
            monkeypatch, {"description": "a cafe", "terms": ["cafe"], "confidence": 0.8}
        )
        await look_at(photograph())
        assert "lower case" in recorder.request.system_prompt.lower()

    async def test_the_model_is_told_not_to_name_anything(self, monkeypatch):
        recorder = use(
            monkeypatch, {"description": "a cafe", "terms": ["cafe"], "confidence": 0.8}
        )
        await look_at(photograph())
        prompt = recorder.request.system_prompt.lower()
        assert "never name" in prompt


class TestWhenItCannotSee:
    async def test_low_confidence_is_reported_rather_than_searched(self, monkeypatch):
        """A page of results chosen by a guess is worse than saying so: the
        explorer cannot tell the difference and assumes the catalogue is thin."""
        use(
            monkeypatch,
            {"description": "too dark to tell", "terms": [], "confidence": 0.1},
        )
        look = await look_at(photograph())
        assert look.unclear

    async def test_an_empty_answer_is_unclear_even_at_high_confidence(self, monkeypatch):
        """A model claiming certainty about nothing is still nothing."""
        use(monkeypatch, {"description": "", "terms": [], "confidence": 0.99})
        assert (await look_at(photograph())).unclear

    async def test_a_clear_photograph_is_not_unclear(self, monkeypatch):
        use(
            monkeypatch,
            {
                "description": "a traditional coffee house",
                "terms": ["coffee", "cafe"],
                "confidence": 0.85,
            },
        )
        look = await look_at(photograph())
        assert not look.unclear
        assert look.query == "coffee cafe"

    async def test_a_missing_confidence_is_treated_as_no_confidence(self, monkeypatch):
        use(monkeypatch, {"description": "a cafe", "terms": ["cafe"]})
        assert (await look_at(photograph())).unclear

    async def test_a_nonsense_confidence_does_not_crash(self, monkeypatch):
        use(
            monkeypatch,
            {"description": "a cafe", "terms": ["cafe"], "confidence": "very sure"},
        )
        assert (await look_at(photograph())).unclear

    def test_the_threshold_is_not_zero(self):
        assert MIN_CONFIDENCE > 0


class TestTheStubNeverPretendsToSee:
    async def test_it_reports_no_confidence_rather_than_describing(self):
        """With no API key there is no vision model. Inventing a plausible
        description of a photograph nobody looked at is the one failure a stub
        must never have - it would be indistinguishable from working."""
        result = await StubProvider().generate(
            GenerationRequest(
                system_prompt="", user_message="", image=(photograph(), "image/jpeg")
            ),
            model="stub",
        )
        assert result.structured == {"description": "", "terms": [], "confidence": 0.0}

    async def test_which_sends_visual_search_down_the_unclear_path(self, monkeypatch):
        monkeypatch.setattr(visual, "get_provider", StubProvider)
        assert (await look_at(photograph())).unclear


class TestThePhotographIsNotKept:
    def test_nothing_in_the_module_writes_it_anywhere(self):
        """Photographs of a city contain people, homes and number plates that
        nobody consented to hand over."""
        source = pathlib.Path(visual.__file__).read_text(encoding="utf-8")
        for forbidden in ("store(", "open(", "Path(", "session.add"):
            assert forbidden not in source, forbidden

    async def test_it_is_re_encoded_before_it_is_sent(self, monkeypatch):
        """`process_image` decodes and re-encodes, which drops EXIF - including
        the GPS coordinates a phone camera writes into every shot."""
        recorder = use(
            monkeypatch, {"description": "a cafe", "terms": ["cafe"], "confidence": 0.8}
        )
        original = photograph(size=(3000, 2000))
        await look_at(original)

        sent, mime = recorder.request.image
        assert mime == "image/jpeg"
        assert sent != original
        # Bounded, so a large upload does not become a large model call.
        width, height = Image.open(io.BytesIO(sent)).size
        assert max(width, height) <= 2000

    async def test_something_that_is_not_an_image_is_refused(self):
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError):
            await look_at(b"this is not a photograph")


class TestItIsAnInputMethodNotASecondSearch:
    def test_the_route_runs_the_ordinary_search(self):
        """Whatever ranking and personalisation the text path gained yesterday,
        this gets today. A parallel retrieval stack is two things to keep in
        step, and they never are."""
        import inspect

        from app.api.routes import discovery

        source = inspect.getsource(discovery.visual_search)
        assert "search_experiences" in source
        assert "_context" in source

    def test_it_is_behind_a_flag(self):
        import inspect

        from app.api.routes import discovery

        assert '"search.visual"' in inspect.getsource(discovery.visual_search)

    def test_it_shares_the_model_budget_rather_than_the_free_search_one(self):
        import inspect

        from app.api.routes import discovery

        assert "CONCIERGE_LIMIT" in inspect.getsource(discovery.visual_search)

    def test_an_unclear_photograph_returns_no_results(self):
        import inspect

        source = inspect.getsource(
            __import__("app.api.routes.discovery", fromlist=["x"]).visual_search
        )
        assert "results=[]" in source


class TestPerception:
    async def test_it_does_not_spend_the_budget_thinking(self, monkeypatch):
        """Looking at a picture is perception, not reasoning, and the thinking
        allowance comes out of the answer."""
        recorder = use(
            monkeypatch, {"description": "a cafe", "terms": ["cafe"], "confidence": 0.8}
        )
        await look_at(photograph())
        assert recorder.request.thinking is False

    async def test_the_temperature_is_low(self, monkeypatch):
        """Description, not invention."""
        recorder = use(
            monkeypatch, {"description": "a cafe", "terms": ["cafe"], "confidence": 0.8}
        )
        await look_at(photograph())
        assert recorder.request.temperature <= 0.2

    async def test_every_field_is_required_of_the_model(self, monkeypatch):
        """An omitted field leaves the caller unable to tell a refusal from a
        lapse - the problem the publisher assistant hit."""
        recorder = use(
            monkeypatch, {"description": "a cafe", "terms": ["cafe"], "confidence": 0.8}
        )
        await look_at(photograph())
        schema = recorder.request.response_schema
        assert set(schema["required"]) == {"description", "terms", "confidence"}


class TestTheLook:
    def test_terms_are_preferred_over_prose_for_the_query(self):
        look = Look(description="a long sentence about a place", terms=["coffee", "cafe"])
        assert look.query == "coffee cafe"

    def test_the_description_is_used_when_there_are_no_terms(self):
        look = Look(description="a rooftop bar", terms=[])
        assert look.query == "a rooftop bar"

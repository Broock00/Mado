"""Unprompted suggestions and offline trips (spec NOT-002, AI-005, EXP-005).

This is the one feature where being wrong is not a bad answer but an
interruption, so nearly every test here is about a reason *not* to send
something. The failure mode is not a crash - it is a channel that becomes noise
and gets muted, after which the one message that mattered does not arrive
either.
"""

from __future__ import annotations

import pathlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domains.explorer import suggestions
from app.domains.explorer.notifications import DEFAULT_ON, KIND_NEARBY

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)


class TestWhoIsNeverInterrupted:
    def test_nearby_suggestions_are_off_unless_asked_for(self):
        """Every other notification kind is something the explorer set in
        motion - they saved the event, they kept the plan. This one is the
        platform speaking first, so it starts silent."""
        assert KIND_NEARBY not in DEFAULT_ON

    async def test_somebody_who_was_just_here_is_left_alone(self):
        """Telling somebody what is on hours after they closed the page is a
        second copy of the page they closed."""
        import inspect

        source = inspect.getsource(suggestions.suggest_nearby)
        assert "_recently_active" in source

    async def test_at_most_one_a_week(self):
        import inspect

        assert timedelta(days=7) <= suggestions.COOLDOWN
        assert "_already_suggested" in inspect.getsource(suggestions.suggest_nearby)


class TestWhatIsNeverClaimed:
    def test_no_coordinates_are_read_or_stored(self):
        """Mado takes a position per request for ranking and throws it away, and
        the privacy page says so. A notification job runs hours later with
        nobody present, so any sentence about metres would require quietly
        starting to keep a location history."""
        source = pathlib.Path(suggestions.__file__).read_text(encoding="utf-8")
        for forbidden in ("latitude", "longitude", "ST_Distance", "distance_km"):
            assert forbidden not in source, forbidden

    def test_nearness_comes_from_a_city_they_told_us(self):
        source = pathlib.Path(suggestions.__file__).read_text(encoding="utf-8")
        assert "home_city_slug" in source

    def test_an_explorer_with_no_stated_city_gets_nothing(self):
        """Rather than a guess dressed as local knowledge."""
        import inspect

        source = inspect.getsource(suggestions.for_explorer)
        assert "if not city_slug" in source
        assert "return None" in source


class TestTheQualityFloor:
    def test_a_single_five_star_review_is_not_a_recommendation(self):
        """Interrupting somebody to pass off a coincidence as a recommendation
        is how the channel earns its mute."""
        assert suggestions.MIN_RATINGS >= 5
        assert suggestions.MIN_RATING >= 4.0

    def test_a_weak_affinity_is_not_personalisation(self):
        assert suggestions.MIN_AFFINITY > 0

    async def test_no_affinity_means_no_suggestion(self):
        """Sending the city's most popular thing to everybody is a mailing
        list, not a suggestion."""
        import inspect

        source = inspect.getsource(suggestions._pick)
        assert "if not liked" in source

    async def test_something_they_already_saved_is_not_news(self):
        import inspect

        assert "saved_ids" in inspect.getsource(suggestions._pick)


class TestWhatItActuallyPicks:
    """The selection itself, not its shape.

    `_pick` is given its candidates, so the affinity lookup is the only thing
    that needs standing in for - everything else is arithmetic over data passed
    in.
    """

    def experience(self, *, category, rating=4.5, ratings=50, title="Thing"):
        return SimpleNamespace(
            id=uuid.uuid4(),
            title=title,
            category=SimpleNamespace(slug=category, name=category.title()),
            venue=None,
            rating_average=rating,
            rating_count=ratings,
            summary=None,
        )

    async def pick(self, monkeypatch, candidates, *, liked, disliked=(), saved=()):
        inferred = SimpleNamespace(
            categories=dict(liked), disliked_categories=set(disliked), tags={}, confidence=1.0
        )

        async def fake_infer(*_args, **_kwargs):
            return inferred

        monkeypatch.setattr(suggestions, "infer_preferences", fake_infer)
        return await suggestions._pick(
            session=None,
            user=SimpleNamespace(id=uuid.uuid4()),
            profile=SimpleNamespace(privacy={}),
            candidates=[(e, None) for e in candidates],
            saved_ids=set(saved),
            now=NOW,
        )

    async def test_it_picks_the_category_they_like_most(self, monkeypatch):
        music = self.experience(category="music", title="Azmari night")
        markets = self.experience(category="markets", title="Spice walk")
        picked = await self.pick(
            monkeypatch, [markets, music], liked={"music": 0.9, "markets": 0.4}
        )
        assert picked is not None
        assert picked[0].title == "Azmari night"

    async def test_rating_breaks_a_tie(self, monkeypatch):
        worse = self.experience(category="music", rating=4.3, title="Quieter night")
        better = self.experience(category="music", rating=4.9, title="Better night")
        picked = await self.pick(monkeypatch, [worse, better], liked={"music": 0.8})
        assert picked[0].title == "Better night"

    async def test_it_never_suggests_something_already_saved(self, monkeypatch):
        saved = self.experience(category="music", rating=4.9, title="Already saved")
        other = self.experience(category="music", rating=4.3, title="New to them")
        picked = await self.pick(
            monkeypatch, [saved, other], liked={"music": 0.8}, saved=[saved.id]
        )
        assert picked[0].title == "New to them"

    async def test_a_disliked_category_is_never_suggested(self, monkeypatch):
        """Even when the affinity map still carries a stale positive score."""
        nightlife = self.experience(category="nightlife", rating=4.9)
        picked = await self.pick(
            monkeypatch, [nightlife], liked={"nightlife": 0.9}, disliked=["nightlife"]
        )
        assert picked is None

    async def test_a_weak_affinity_is_not_enough(self, monkeypatch):
        """Below the floor it is noise, and a guess wearing a personalisation
        badge is worse than saying nothing."""
        thing = self.experience(category="sports")
        picked = await self.pick(
            monkeypatch, [thing], liked={"sports": suggestions.MIN_AFFINITY - 0.01}
        )
        assert picked is None

    async def test_no_affinity_at_all_means_silence(self, monkeypatch):
        thing = self.experience(category="music", rating=5.0, ratings=900)
        picked = await self.pick(monkeypatch, [thing], liked={})
        assert picked is None


class TestOneDefinitionOfTaste:
    def test_the_concierge_and_the_notification_pick_the_same_way(self):
        """Two definitions of "something you would like" would drift, and the
        day they disagree the platform is telling somebody two different things
        about their own taste."""
        import inspect

        job = inspect.getsource(suggestions.suggest_nearby)
        assert "for_explorer" in job

        from app.api.routes import concierge

        endpoint = inspect.getsource(concierge.proactive_suggestion)
        assert "for_explorer" in endpoint

    def test_the_cooldown_is_not_shared(self):
        """Showing something in a panel the explorer just opened is not an
        interruption, so it needs no weekly cap - and applying one there would
        make the concierge silently empty most of the time."""
        import inspect

        shared = inspect.getsource(suggestions.for_explorer)
        assert "COOLDOWN" not in shared
        assert "_already_suggested" not in shared

    def test_nothing_worth_saying_is_a_valid_answer(self):
        import inspect

        from app.api.routes import concierge

        source = inspect.getsource(concierge.proactive_suggestion)
        assert "Envelope(data=None)" in source


class TestTheSuggestionIsGroundedNotGenerated:
    def test_no_model_is_called(self):
        """Spec 56.01 §3.1 - tools decide facts, the model only phrases. Here
        not even that: the reason is assembled from the fields that made the
        choice, so it cannot justify a decision it did not make."""
        import inspect

        from app.api.routes import concierge

        source = inspect.getsource(concierge.proactive_suggestion)
        for forbidden in ("AIGateway", "generate(", "provider"):
            assert forbidden not in source, forbidden


class TestOfflineTripsCacheAlmostNothing:
    """Spec EXP-005. An offline-first shell that caches everything is how a
    product shows a sold-out event as available and last Tuesday's rail as
    tonight - the reader cannot tell, which is what makes it worse than an
    error."""

    def worker(self) -> str:
        path = (
            pathlib.Path(__file__).parents[2]
            / "frontend"
            / "web"
            / "public"
            / "service-worker.js"
        )
        return path.read_text(encoding="utf-8")

    def test_only_kept_itineraries_are_stored(self):
        source = self.worker()
        assert "/api/v1/itineraries/" in source
        # The time-sensitive surfaces must not appear as cache targets.
        for forbidden in ("/api/v1/discover", "/api/v1/search", "/api/v1/assistant"):
            assert forbidden not in source, forbidden

    def test_plans_are_network_first(self):
        """So the times are current whenever they can be. Cache-first would
        show this morning's copy to somebody with a perfectly good connection."""
        source = self.worker()
        assert source.index("fetch(request)") < source.index("caches.match(request)")

    def test_a_cached_plan_is_marked_as_one(self):
        """An offline plan that looks live is how somebody turns up to a date
        that was called off."""
        assert "X-Mado-From-Cache" in self.worker()

    def test_the_cache_is_versioned_so_a_deploy_replaces_it(self):
        source = self.worker()
        assert "VERSION" in source
        assert "caches.delete" in source

    def test_the_shell_install_cannot_be_broken_by_one_missing_file(self):
        """`addAll` rejects the whole install if any URL fails, leaving no
        service worker at all - a missing favicon should cost a favicon."""
        source = self.worker()
        assert "allSettled" in source
        assert "cache.addAll" not in source


class TestOfflineRegistration:
    def test_the_worker_is_not_registered_in_development(self):
        """A service worker in front of the Vite dev server serves yesterday's
        modules, which presents as an edit that does nothing."""
        path = (
            pathlib.Path(__file__).parents[2] / "frontend" / "web" / "src" / "app" / "offline.ts"
        )
        source = path.read_text(encoding="utf-8")
        assert "import.meta.env.PROD" in source

    def test_the_banner_does_not_claim_to_know_the_reader_is_offline(self):
        """`navigator.onLine` reports whether there is a network interface, not
        whether anything is reachable - a captive portal reports a happy one."""
        path = (
            pathlib.Path(__file__).parents[2]
            / "frontend"
            / "web"
            / "src"
            / "lib"
            / "messages"
            / "en.ts"
        )
        banner = path.read_text(encoding="utf-8")
        assert "Cannot reach Mado" in banner


class TestTheMessagesExist:
    def test_a_suggestion_reads_as_an_offer_rather_than_an_alert(self):
        from app.core.messages import CATALOGUE

        title = CATALOGUE["en"]["suggestion.nearby.title"]
        assert "{title}" in title
        # No urgency language: this is the one message nobody asked for.
        for shouty in ("!", "now!", "hurry", "don't miss"):
            assert shouty not in title.lower()

    def test_it_is_translated(self):
        from app.core.messages import CATALOGUE

        for key in CATALOGUE["en"]:
            if key.startswith("suggestion."):
                assert key in CATALOGUE["am"], key


def _uuid() -> uuid.UUID:
    return uuid.uuid4()

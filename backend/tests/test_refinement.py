"""Plan refinement tests (spec AI-004).

The property that matters is not that a refinement produces *a* plan - it is
that it produces the same plan with one thing changed. An explorer who says
"make it cheaper" and receives a different evening has lost the stops they had
already agreed to, and learns that the concierge is a slot machine.

So most of these assert what survives, not what moves.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.domains.ai.refinement import (
    CHEAPER,
    CHEAPER_FACTOR,
    EARLIER,
    KINDS,
    LATER,
    LONGER,
    REMOVE_STOP,
    REPLACE_STOP,
    SHIFT,
    SHORTER,
    SOMETHING_ELSE,
    PlanDiff,
    Refinement,
    apply,
    diff_plans,
    from_model,
    read_rules,
)
from app.domains.explorer.planning import MAX_STOPS, PlanRequest

START = datetime(2026, 8, 9, 17, 0, tzinfo=UTC)


def request(**overrides) -> PlanRequest:
    return PlanRequest(
        **{
            "start": START,
            "end": START + timedelta(hours=5),
            "city_slug": "addis-ababa",
            "budget": 600.0,
            "max_stops": 3,
            "categories": ["music"],
            **overrides,
        }
    )


STOPS = [uuid.uuid4() for _ in range(3)]


class TestReadingWhatWasAsked:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("make it cheaper", CHEAPER),
            ("that's too expensive", CHEAPER),
            ("can we start later?", LATER),
            ("a bit earlier please", EARLIER),
            ("this is too packed", SHORTER),
            ("add another stop", LONGER),
            ("swap the last one", REPLACE_STOP),
            ("drop the first stop", REMOVE_STOP),
            ("show me something else entirely", SOMETHING_ELSE),
        ],
    )
    def test_common_phrasings(self, text, expected):
        read = read_rules(text, stop_count=3)
        assert read is not None and read.kind == expected

    def test_a_bare_amount_is_a_budget_refinement(self):
        """"Keep it under 300 birr" contains no cheapness word at all."""
        read = read_rules("keep it under 300 birr", stop_count=3)
        assert read is not None
        assert read.kind == CHEAPER
        assert read.budget == 300.0

    def test_start_over_is_not_read_as_start_earlier(self):
        """"Start over" contains "start", which the EARLIER pattern would claim."""
        read = read_rules("let's start over", stop_count=3)
        assert read is not None and read.kind == SOMETHING_ELSE

    def test_positions_are_resolved_against_the_plan_shown(self):
        assert read_rules("swap the last one", stop_count=3).stop_index == 2
        assert read_rules("swap the last one", stop_count=2).stop_index == 1
        assert read_rules("change the second", stop_count=3).stop_index == 1

    def test_an_unrelated_message_is_not_a_refinement(self):
        """Otherwise every follow-up mangles the plan."""
        assert read_rules("what is the weather like", stop_count=3) is None

    def test_the_model_reading_is_held_to_the_closed_set(self):
        assert from_model({"kind": "make_it_fancier"}, stop_count=3) is None
        assert from_model({"kind": CHEAPER}, stop_count=3).kind == CHEAPER

    def test_an_out_of_range_stop_index_is_dropped_not_clamped(self):
        """Clamping would change a stop the explorer did not point at."""
        read = from_model({"kind": REPLACE_STOP, "stopIndex": 9}, stop_count=3)
        assert read is not None and read.stop_index is None


class TestConstraintsCarryForward:
    """The previous request is the base, not the latest message."""

    def test_a_cheaper_plan_is_still_the_same_evening(self):
        refined = apply(
            Refinement(kind=CHEAPER), request(), stop_experience_ids=STOPS, current_cost=400
        )
        assert refined.start == START
        assert refined.categories == ["music"]
        assert refined.city_slug == "addis-ababa"

    def test_cheaper_works_from_what_the_plan_costs_not_the_budget(self):
        """A 310-birr evening under a 600 budget must still get cheaper.

        Trimming the budget alone changes nothing, and the explorer is told
        "that did not change anything" when they asked plainly for cheaper.
        """
        refined = apply(
            Refinement(kind=CHEAPER), request(budget=600.0), stop_experience_ids=STOPS,
            current_cost=310.0,
        )
        assert refined.budget == pytest.approx(310.0 * CHEAPER_FACTOR)
        assert refined.budget < 310.0

    def test_an_explicit_figure_wins(self):
        refined = apply(
            Refinement(kind=CHEAPER, budget=200.0), request(), stop_experience_ids=STOPS,
            current_cost=400,
        )
        assert refined.budget == 200.0

    def test_later_moves_the_whole_window_not_just_the_start(self):
        refined = apply(Refinement(kind=LATER), request(), stop_experience_ids=STOPS)
        assert refined.start == START + SHIFT
        assert refined.end == request().end + SHIFT

    def test_earlier_moves_it_back(self):
        refined = apply(Refinement(kind=EARLIER), request(), stop_experience_ids=STOPS)
        assert refined.start == START - SHIFT

    def test_longer_does_not_exceed_the_planner_ceiling(self):
        refined = apply(
            Refinement(kind=LONGER), request(max_stops=MAX_STOPS), stop_experience_ids=STOPS
        )
        assert refined.max_stops == MAX_STOPS


class TestWhatSurvives:
    """The point of the feature."""

    def test_unmentioned_stops_are_pinned(self):
        refined = apply(
            Refinement(kind=CHEAPER), request(), stop_experience_ids=STOPS, current_cost=400
        )
        assert refined.keep_experience_ids == STOPS

    def test_a_replaced_stop_is_dropped_and_the_rest_kept(self):
        refined = apply(
            Refinement(kind=REPLACE_STOP, stop_index=2), request(), stop_experience_ids=STOPS
        )
        assert refined.keep_experience_ids == STOPS[:2]

    def test_a_replaced_stop_cannot_come_back(self):
        """"Not that one" has to stick, even if the ranker still likes it."""
        refined = apply(
            Refinement(kind=REPLACE_STOP, stop_index=0), request(), stop_experience_ids=STOPS
        )
        assert STOPS[0] in refined.avoid_experience_ids

    def test_rejections_accumulate_across_turns(self):
        first = apply(
            Refinement(kind=REPLACE_STOP, stop_index=0), request(), stop_experience_ids=STOPS
        )
        second = apply(
            Refinement(kind=REPLACE_STOP, stop_index=0),
            first,
            stop_experience_ids=first.keep_experience_ids,
        )
        assert STOPS[0] in second.avoid_experience_ids
        assert STOPS[1] in second.avoid_experience_ids

    def test_removing_shortens_rather_than_backfilling(self):
        """They asked for one fewer, not for a substitute."""
        refined = apply(
            Refinement(kind=REMOVE_STOP, stop_index=1), request(), stop_experience_ids=STOPS
        )
        assert len(refined.keep_experience_ids) == 2
        assert refined.max_stops == 2

    def test_shorter_drops_from_the_end(self):
        """The first stop is usually the anchor the evening was built around."""
        refined = apply(Refinement(kind=SHORTER), request(), stop_experience_ids=STOPS)
        assert refined.keep_experience_ids == STOPS[:2]

    def test_something_else_keeps_nothing_and_avoids_everything(self):
        """Otherwise "something completely different" returns the same evening."""
        refined = apply(Refinement(kind=SOMETHING_ELSE), request(), stop_experience_ids=STOPS)
        assert refined.keep_experience_ids == []
        assert set(STOPS) <= set(refined.avoid_experience_ids)

    def test_an_unresolvable_stop_reference_changes_nothing(self):
        """Changing the wrong stop is worse than changing none."""
        refined = apply(
            Refinement(kind=REPLACE_STOP, stop_index=None), request(), stop_experience_ids=STOPS
        )
        assert refined.keep_experience_ids == STOPS
        assert refined.avoid_experience_ids == []


class TestDescribingTheChange:
    def before(self):
        return {
            "stops": [{"title": "Fendika"}, {"title": "Tomoca"}, {"title": "Meskel Square"}],
            "totalCost": 310.0,
        }

    def test_it_names_what_went_and_what_arrived(self):
        after = {"stops": [{"title": "Fendika"}, {"title": "Meskel Square"}], "totalCost": 60.0}
        diff = diff_plans(self.before(), after)
        assert diff.removed == ["Tomoca"]
        assert diff.kept_count == 2
        assert diff.cost_delta == pytest.approx(-250.0)

    def test_an_unchanged_plan_says_so_rather_than_implying_success(self):
        """Claiming "I made it cheaper" over an identical plan is a lie the
        explorer can check in one glance."""
        diff = diff_plans(self.before(), self.before())
        assert diff.is_unchanged
        assert "did not change anything" in diff.describe()

    def test_proper_nouns_keep_their_capitals(self):
        """`.capitalize()` on the whole sentence turned "Ethiopian Coffee
        Ceremony" into "ethiopian coffee ceremony"."""
        sentence = PlanDiff(removed=["Ethiopian Coffee Ceremony"], cost_delta=-250).describe()
        assert "Ethiopian Coffee Ceremony" in sentence

    def test_a_more_expensive_result_is_not_dressed_up(self):
        sentence = PlanDiff(added=["Somewhere Pricey"], cost_delta=120).describe()
        assert "more" in sentence
        assert "cheaper" not in sentence


class TestTheClosedSet:
    def test_every_kind_is_handled_by_apply(self):
        """A kind nobody applied would silently return the plan unchanged."""
        for kind in KINDS:
            refined = apply(
                Refinement(kind=kind, stop_index=0), request(), stop_experience_ids=STOPS,
                current_cost=400,
            )
            assert isinstance(refined, PlanRequest)

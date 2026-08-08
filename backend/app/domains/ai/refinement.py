"""Adjusting a plan that has already been offered (spec AI-004).

"Make it cheaper." "Swap the last one." "Can we start later?" Ordinary things to
say about a plan, and all of them meaningless without the plan they refer to.

The rule this module exists to enforce: **a refinement changes what was asked
about and nothing else.**

Re-planning from scratch is easy and wrong. An explorer who says "make it
cheaper" and receives a completely different evening has lost the two stops they
had already agreed to, for no reason they can see, and learns that talking to the
concierge is a slot machine rather than a conversation. So a refinement carries
the original constraints forward, applies one delta, and pins everything the
explorer did not object to.

Three consequences:

* **The previous request is the base.** Someone who said "Friday evening, under
  500 birr, somewhere near Bole" and then says "make it cheaper" still means
  Friday evening near Bole. Only the budget moves.
* **Unmentioned stops are kept.** They go into `keep_experience_ids` and survive
  the re-solve, including filters they would now fail - the explorer accepted
  that stop knowing what it cost.
* **A rejected stop is not silently reconsidered.** "Not that one" puts it in
  `avoid_experience_ids`, where the ranker cannot put it back.

Interpretation is model-driven with a deterministic fallback, matching the rest
of the concierge: the fallback covers the phrasings people actually use most and
keeps refinement working when the provider is down.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from app.core.logging import get_logger
from app.domains.explorer.planning import MAX_STOPS, PlanRequest

logger = get_logger("mado.ai.refinement")

# What an explorer can ask for. A closed set: an unrecognised kind is treated as
# no refinement at all rather than guessed at, because guessing wrong here throws
# away a plan somebody liked.
CHEAPER = "cheaper"
EARLIER = "earlier"
LATER = "later"
SHORTER = "shorter"
LONGER = "longer"
REPLACE_STOP = "replace_stop"
REMOVE_STOP = "remove_stop"
SOMETHING_ELSE = "something_else"

KINDS = frozenset(
    {CHEAPER, EARLIER, LATER, SHORTER, LONGER, REPLACE_STOP, REMOVE_STOP, SOMETHING_ELSE}
)

# How far "earlier" and "later" move a window. An hour is what people mean when
# they say it without a number; anything larger silently rewrites their evening.
SHIFT = timedelta(hours=1)

# What "cheaper" means when no figure is given: a quarter off, not half. A large
# cut tends to empty the plan, and an explorer who wanted it empty would have
# said "free".
CHEAPER_FACTOR = 0.75


@dataclass(slots=True)
class Refinement:
    kind: str
    # Which stop the explorer meant, when they meant one. Position in the plan
    # they were shown, zero-based - that is how people refer to stops ("the last
    # one", "the second"), and it survives a title the model misremembers.
    stop_index: int | None = None
    # An explicit figure when they gave one: "under 300 birr".
    budget: float | None = None
    # Set when the reading came from the rules rather than the model.
    degraded: bool = False

    @property
    def keeps_other_stops(self) -> bool:
        """Whether the stops not mentioned should survive.

        False only for "something else entirely", which is the one phrasing that
        genuinely asks for a fresh plan.
        """
        return self.kind != SOMETHING_ELSE


# --- deterministic reading ---------------------------------------------------
#
# Used when the model is unavailable. Not a general parser: it covers the
# phrasings that dominate, and anything else falls through to no refinement,
# which leaves the existing plan alone rather than mangling it.

_LAST = re.compile(r"\b(last|final|end)\b")
_FIRST = re.compile(r"\b(first|start)\b")
_SECOND = re.compile(r"\bsecond\b")
_THIRD = re.compile(r"\bthird\b")

_RULES: list[tuple[str, re.Pattern[str]]] = [
    (CHEAPER, re.compile(r"\b(cheap(er)?|less money|too expensive|lower budget|afford)\b")),
    (EARLIER, re.compile(r"\b(earlier|sooner|start before)\b")),
    (LATER, re.compile(r"\b(later|start after|push (it )?back)\b")),
    (
        SHORTER,
        re.compile(
            r"\b(shorter|fewer stops|too much|too long|too packed|less packed|"
            r"too busy|too rushed|drop one)\b"
        ),
    ),
    (LONGER, re.compile(r"\b(longer|more stops|add (one|another|a stop))\b")),
    (REMOVE_STOP, re.compile(r"\b(remove|drop|cut|take out|skip)\b")),
    (REPLACE_STOP, re.compile(r"\b(swap|replace|change|different|instead of|not that)\b")),
    (SOMETHING_ELSE, re.compile(r"\b(something else|start over|completely different|redo)\b")),
]

_AMOUNT = re.compile(r"\b(?:under|below|max(?:imum)?|less than)\s+(\d[\d,]*)\b")


def _stop_index(text: str, stop_count: int) -> int | None:
    if stop_count <= 0:
        return None
    if _LAST.search(text):
        return stop_count - 1
    if _FIRST.search(text):
        return 0
    if _SECOND.search(text) and stop_count > 1:
        return 1
    if _THIRD.search(text) and stop_count > 2:
        return 2
    return None


def read_rules(text: str, *, stop_count: int) -> Refinement | None:
    """Interpret a refinement without a model. None when nothing matched."""
    lowered = text.lower()

    # Checked before the generic rules: "start over" contains "start", which the
    # EARLIER pattern would otherwise claim.
    if _RULES[-1][1].search(lowered):
        return Refinement(kind=SOMETHING_ELSE, degraded=True)

    amount = _AMOUNT.search(lowered)

    for kind, pattern in _RULES[:-1]:
        if not pattern.search(lowered):
            continue
        return Refinement(
            kind=kind,
            stop_index=_stop_index(lowered, stop_count)
            if kind in {REPLACE_STOP, REMOVE_STOP}
            else None,
            budget=float(amount.group(1).replace(",", "")) if amount else None,
            degraded=True,
        )

    # A figure on its own is a budget refinement. "Keep it under 300 birr"
    # contains none of the words above and is one of the most natural ways to
    # ask, so naming a number counts as asking for cheaper.
    if amount:
        return Refinement(
            kind=CHEAPER,
            budget=float(amount.group(1).replace(",", "")),
            degraded=True,
        )
    return None


def from_model(payload: dict[str, Any] | None, *, stop_count: int) -> Refinement | None:
    """Adapt the model's reading, dropping anything outside the closed set."""
    if not isinstance(payload, dict):
        return None

    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        return None

    index = payload.get("stopIndex")
    stop_index = index if isinstance(index, int) and 0 <= index < stop_count else None

    budget = payload.get("budget")
    try:
        amount = float(budget) if budget is not None else None
    except (TypeError, ValueError):
        amount = None

    return Refinement(kind=kind, stop_index=stop_index, budget=amount)


# --- applying ----------------------------------------------------------------


def apply(
    refinement: Refinement,
    previous: PlanRequest,
    *,
    stop_experience_ids: list[uuid.UUID],
    current_cost: float | None = None,
) -> PlanRequest:
    """Produce the request for the refined plan.

    `previous` is the request that produced the plan being refined, which is what
    carries the explorer's original constraints forward. `stop_experience_ids` is
    the plan they were shown, in order.
    """
    keep = list(stop_experience_ids)
    avoid = list(previous.avoid_experience_ids)
    request = previous

    if refinement.kind == SOMETHING_ELSE:
        # The one case that keeps nothing. Everything already offered goes on the
        # avoid list, or "something else" returns the same evening.
        return replace(
            request,
            keep_experience_ids=[],
            avoid_experience_ids=avoid + keep,
        )

    if refinement.kind == CHEAPER:
        target = refinement.budget
        if target is None:
            # Worked down from what the plan actually costs, not from the stated
            # budget. An evening costing 310 against a 600 budget is already well
            # under it, so trimming the budget changes nothing and the explorer
            # is told "that did not change anything" when they asked plainly for
            # something cheaper.
            if current_cost and current_cost > 0:
                target = round(current_cost * CHEAPER_FACTOR, 2)
            elif previous.budget:
                target = previous.budget * CHEAPER_FACTOR
        # Only fall back to free when there was no cost to work down from at all.
        request = replace(request, budget=target, free_only=target is None)

    elif refinement.kind == EARLIER:
        request = replace(request, start=previous.start - SHIFT, end=previous.end - SHIFT)

    elif refinement.kind == LATER:
        request = replace(request, start=previous.start + SHIFT, end=previous.end + SHIFT)

    elif refinement.kind == SHORTER:
        target = max(1, min(previous.max_stops, len(keep)) - 1)
        request = replace(request, max_stops=target)
        # Drop from the end. The later stops are the ones people cut when an
        # evening is too long - the first is usually the anchor they wanted.
        keep = keep[:target]

    elif refinement.kind == LONGER:
        request = replace(request, max_stops=min(MAX_STOPS, previous.max_stops + 1))

    elif refinement.kind in {REPLACE_STOP, REMOVE_STOP}:
        index = refinement.stop_index
        if index is None or not (0 <= index < len(keep)):
            # They meant a stop and we could not tell which. Changing the wrong
            # one is worse than changing none, so the plan is left alone and the
            # caller asks.
            return replace(request, keep_experience_ids=keep, avoid_experience_ids=avoid)

        rejected = keep.pop(index)
        avoid = avoid + [rejected]
        if refinement.kind == REMOVE_STOP:
            # Removed, not replaced: the plan gets shorter rather than backfilled
            # with something they did not ask for.
            request = replace(request, max_stops=max(1, len(keep)))

    return replace(request, keep_experience_ids=keep, avoid_experience_ids=avoid)


# --- describing the change ---------------------------------------------------


@dataclass(slots=True)
class PlanDiff:
    """What actually changed, worked out by comparing the two plans.

    Derived rather than taken from the model's own account of what it did. An
    explorer comparing two lists of four stops by eye will not spot that the
    third one moved, and the concierge saying "I made it cheaper" when the total
    went up would be worse than saying nothing.
    """

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    cost_delta: float = 0.0
    kept_count: int = 0

    @property
    def is_unchanged(self) -> bool:
        return not self.added and not self.removed and abs(self.cost_delta) < 0.01

    def describe(self) -> str:
        """One plain sentence, or an admission that nothing moved."""
        if self.is_unchanged:
            return "That did not change anything - this is the same plan."

        parts: list[str] = []
        if self.removed:
            parts.append(f"dropped {_join(self.removed)}")
        if self.added:
            parts.append(f"added {_join(self.added)}")
        if self.cost_delta < -0.01:
            parts.append(f"about {abs(self.cost_delta):.0f} birr cheaper")
        elif self.cost_delta > 0.01:
            parts.append(f"about {self.cost_delta:.0f} birr more")

        # Only the first character. `.capitalize()` lowercases everything after
        # it, which turns "Ethiopian Coffee Ceremony" into "ethiopian coffee
        # ceremony" - the titles here are proper nouns and places.
        joined = "; ".join(parts)
        sentence = joined[:1].upper() + joined[1:]
        if self.kept_count:
            sentence += f". Kept the other {self.kept_count}."
        return sentence + ("" if sentence.endswith(".") else ".")


def diff_plans(before: dict[str, Any], after: dict[str, Any]) -> PlanDiff:
    """Compare two rendered plans by the titles the explorer actually saw."""
    before_stops = {s.get("title", "") for s in (before.get("stops") or [])}
    after_stops = {s.get("title", "") for s in (after.get("stops") or [])}

    return PlanDiff(
        added=sorted(after_stops - before_stops),
        removed=sorted(before_stops - after_stops),
        cost_delta=float(after.get("totalCost") or 0) - float(before.get("totalCost") or 0),
        kept_count=len(before_stops & after_stops),
    )


def _join(titles: list[str]) -> str:
    if len(titles) == 1:
        return titles[0]
    return ", ".join(titles[:-1]) + f" and {titles[-1]}"

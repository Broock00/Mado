"""Intent taxonomy, classification and temporal resolution.

The intent list is taken verbatim from spec 56.01 s8. Classification is rule-based
first and model-assisted only when the rules are unsure, for three reasons the
specs push toward: it is free, it is deterministic (spec 56.01 s3.3 wants
structured execution rather than uncontrolled generation), and it cannot fail when
a provider is down.

Temporal resolution follows spec 56.02 s24-33, which treats "tonight" and "this
weekend" as genuinely ambiguous and worth resolving explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

# Spec 56.01 s8.
DISCOVER_EVENTS = "DISCOVER_EVENTS"
SEARCH_EVENTS = "SEARCH_EVENTS"
SEARCH_EXPERIENCES = "SEARCH_EXPERIENCES"
PLAN_ACTIVITY = "PLAN_ACTIVITY"
RECOMMEND_ACTIVITY = "RECOMMEND_ACTIVITY"
GET_EVENT_DETAILS = "GET_EVENT_DETAILS"
COMPARE_EVENTS = "COMPARE_EVENTS"
SAVE_EVENT = "SAVE_EVENT"
ASK_ABOUT_VENUE = "ASK_ABOUT_VENUE"
GENERAL_ASSISTANCE = "GENERAL_ASSISTANCE"

# Spec 56.01 s10.
CONFIDENCE_HIGH = 0.90
CONFIDENCE_MODERATE = 0.70
CONFIDENCE_LOW = 0.50


@dataclass(slots=True)
class TimeWindow:
    start: datetime
    end: datetime
    label: str
    # Below ~0.7 the concierge should confirm rather than assume (spec 56.02 s33).
    confidence: float = 1.0


@dataclass(slots=True)
class Classification:
    intent: str
    confidence: float
    entities: dict = field(default_factory=dict)
    constraints: dict = field(default_factory=dict)
    time_window: TimeWindow | None = None
    requires_tools: bool = True

    @property
    def is_ambiguous(self) -> bool:
        return self.confidence < CONFIDENCE_LOW


# Ordered: the first match wins, so narrower intents are listed before broader ones.
_INTENT_PATTERNS: list[tuple[str, float, re.Pattern[str]]] = [
    (
        PLAN_ACTIVITY,
        0.93,
        re.compile(
            r"\b(plan|itinerary|schedule|organi[sz]e)\b.*\b(day|evening|weekend|trip|afternoon|night)\b"
            r"|\bplan (me|my|a)\b",
            re.I,
        ),
    ),
    (
        COMPARE_EVENTS,
        0.9,
        re.compile(r"\b(compare|versus|vs\.?|which (one|is better)|better than)\b", re.I),
    ),
    (SAVE_EVENT, 0.9, re.compile(r"\b(save|bookmark|add to (my )?(saved|list))\b", re.I)),
    (
        ASK_ABOUT_VENUE,
        0.85,
        re.compile(
            r"\b(where is|address of|how do i get to|opening hours"
            r"|parking|accessible|wheelchair)\b",
            re.I,
        ),
    ),
    (
        GET_EVENT_DETAILS,
        0.85,
        re.compile(
            r"\b(tell me more|more (info|details)|what time does|how much (is|does)|how long)\b",
            re.I,
        ),
    ),
    (
        RECOMMEND_ACTIVITY,
        0.88,
        re.compile(
            r"\b(recommend|suggest|what should i|any ideas|surprise me"
            r"|i'?m bored|something to do)\b",
            re.I,
        ),
    ),
    (
        DISCOVER_EVENTS,
        0.86,
        re.compile(
            r"\b(what'?s (on|happening)|anything (on|happening)|events?)\b"
            r".*\b(tonight|today|now|weekend|tomorrow)\b"
            r"|\b(what'?s (on|happening))\b",
            re.I,
        ),
    ),
    (
        SEARCH_EVENTS,
        0.8,
        re.compile(
            r"\b(find|search|look for|show me)\b.*\b(events?|concerts?|gigs?|shows?)\b", re.I
        ),
    ),
    (SEARCH_EXPERIENCES, 0.78, re.compile(r"\b(find|search|look for|show me|where can i)\b", re.I)),
]

# Category hints let the concierge filter before ranking (spec 56.02 s15).
_CATEGORY_KEYWORDS = {
    "food-drink": [
        "restaurant",
        "eat",
        "food",
        "dinner",
        "lunch",
        "brunch",
        "coffee",
        "cafe",
        "café",
        "bar",
        "drink",
    ],
    "music": ["music", "concert", "gig", "jazz", "live band", "dj", "club night"],
    "arts-culture": [
        "museum",
        "gallery",
        "art",
        "exhibition",
        "theatre",
        "theater",
        "culture",
        "history",
    ],
    "outdoors": ["park", "hike", "hiking", "walk", "outdoor", "nature", "garden", "trail"],
    "nightlife": ["nightlife", "club", "party", "night out", "dancing"],
    "sports": ["sport", "football", "running", "gym", "match", "fitness"],
    "family": ["family", "kids", "children", "child-friendly"],
    "learning": ["workshop", "class", "course", "talk", "lecture", "hackathon", "conference"],
    "markets": ["market", "shopping", "bazaar", "craft"],
}

_FREE_PATTERN = re.compile(r"\b(free|no cost|costs? nothing|zero birr)\b", re.I)
_NEARBY_PATTERN = re.compile(
    r"\b(near(by| me)?|close by|around here|walking distance|within walking)\b", re.I
)
_INDOOR_PATTERN = re.compile(r"\b(indoor|inside|out of the rain|sheltered)\b", re.I)
_OUTDOOR_PATTERN = re.compile(r"\b(outdoor|outside|open air|in the sun)\b", re.I)
_BUDGET_PATTERN = re.compile(r"\b(cheap|budget|affordable|inexpensive|low cost)\b", re.I)
_NEGATION_PATTERN = re.compile(r"\b(not|no|don'?t|avoid|without|except|rather not)\b\s+(\w+)", re.I)


def resolve_time_window(
    text: str, *, now: datetime, timezone: str = "Africa/Addis_Ababa"
) -> TimeWindow | None:
    """Resolve a relative time expression against the *city's* clock.

    Spec 56.02 s28 is specific that timezone comes from the city, not the device:
    an explorer planning an Addis Ababa evening from London means Addis evening.
    """
    try:
        local_now = now.astimezone(ZoneInfo(timezone))
    except Exception:  # noqa: BLE001 - unknown zone should not break classification
        local_now = now.astimezone(UTC)

    lowered = text.casefold()

    def _local(dt: datetime) -> datetime:
        return dt.astimezone(UTC)

    if "right now" in lowered or re.search(r"\bnow\b", lowered):
        return TimeWindow(_local(local_now), _local(local_now + timedelta(hours=3)), "right now")

    if "tonight" in lowered or "this evening" in lowered:
        start = (
            local_now
            if local_now.hour >= 16
            else local_now.replace(hour=17, minute=0, second=0, microsecond=0)
        )
        # "Tonight" runs past midnight - ending at 00:00 would drop late events.
        end = (local_now + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
        return TimeWindow(_local(start), _local(end), "tonight")

    if "tomorrow" in lowered:
        base = local_now + timedelta(days=1)
        return TimeWindow(
            _local(base.replace(hour=0, minute=0, second=0, microsecond=0)),
            _local(base.replace(hour=23, minute=59, second=59, microsecond=0)),
            "tomorrow",
        )

    if "this weekend" in lowered or re.search(r"\bweekend\b", lowered):
        weekday = local_now.weekday()
        days_until_friday = 0 if weekday >= 4 else (4 - weekday)
        friday = (local_now + timedelta(days=days_until_friday)).replace(
            hour=17, minute=0, second=0, microsecond=0
        )
        if weekday >= 4 and local_now > friday:
            friday = local_now
        sunday_end = (friday + timedelta(days=(6 - friday.weekday()))).replace(
            hour=23, minute=59, second=59, microsecond=0
        )
        return TimeWindow(_local(friday), _local(sunday_end), "this weekend")

    if "today" in lowered:
        return TimeWindow(
            _local(local_now),
            _local(local_now.replace(hour=23, minute=59, second=59, microsecond=0)),
            "today",
        )

    if "this week" in lowered:
        return TimeWindow(_local(local_now), _local(local_now + timedelta(days=7)), "this week")

    # "Next Friday" is genuinely ambiguous (spec 56.02 s32) - resolve to the
    # nearest reading but flag low confidence so the caller can confirm.
    weekday_match = re.search(
        r"\b(next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered
    )
    if weekday_match:
        names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        target = names.index(weekday_match.group(2))
        delta = (target - local_now.weekday()) % 7
        if delta == 0:
            delta = 7
        if weekday_match.group(1):  # explicit "next"
            delta += 7 if delta <= 7 else 0
        day = local_now + timedelta(days=delta)
        return TimeWindow(
            _local(day.replace(hour=0, minute=0, second=0, microsecond=0)),
            _local(day.replace(hour=23, minute=59, second=59, microsecond=0)),
            weekday_match.group(0),
            confidence=0.65 if weekday_match.group(1) else 0.85,
        )

    return None


def extract_constraints(text: str) -> dict:
    """Pull hard and soft constraints out of the message (spec 56.02 s19-23)."""
    constraints: dict = {}

    if _FREE_PATTERN.search(text):
        constraints["free_only"] = True
    if _BUDGET_PATTERN.search(text):
        constraints["budget"] = "budget"
    if _NEARBY_PATTERN.search(text):
        constraints["nearby"] = True
    if _INDOOR_PATTERN.search(text):
        constraints["indoor"] = True
    elif _OUTDOOR_PATTERN.search(text):
        constraints["indoor"] = False

    categories = [
        slug
        for slug, keywords in _CATEGORY_KEYWORDS.items()
        if any(keyword in text.casefold() for keyword in keywords)
    ]

    # Negations are separated rather than merged: "somewhere to eat, not a bar"
    # must not filter down to bars (spec 56.02 s20).
    excluded: list[str] = []
    for _, noun in _NEGATION_PATTERN.findall(text):
        for slug, keywords in _CATEGORY_KEYWORDS.items():
            if noun.casefold() in keywords and slug in categories:
                categories.remove(slug)
                excluded.append(slug)

    if categories:
        constraints["categories"] = categories
    if excluded:
        constraints["excluded_categories"] = excluded

    group = re.search(r"\b(\d+)\s+(people|of us|friends|guests)\b", text, re.I)
    if group:
        constraints["group_size"] = int(group.group(1))
    if re.search(r"\b(with (my )?(kids|children|family)|family[- ]friendly)\b", text, re.I):
        constraints["family_friendly"] = True

    return constraints


def classify(text: str, *, now: datetime, timezone: str = "Africa/Addis_Ababa") -> Classification:
    """Classify a message into a structured intent."""
    cleaned = text.strip()
    if not cleaned:
        return Classification(intent=GENERAL_ASSISTANCE, confidence=0.0, requires_tools=False)

    intent = GENERAL_ASSISTANCE
    confidence = 0.4
    for candidate, score, pattern in _INTENT_PATTERNS:
        if pattern.search(cleaned):
            intent, confidence = candidate, score
            break

    time_window = resolve_time_window(cleaned, now=now, timezone=timezone)
    constraints = extract_constraints(cleaned)

    # A concrete time reference is strong evidence the explorer wants events, and
    # nudges a borderline classification over the confidence line.
    if time_window is not None and intent in {GENERAL_ASSISTANCE, SEARCH_EXPERIENCES}:
        intent = DISCOVER_EVENTS
        confidence = max(confidence, 0.82)

    if constraints.get("categories") and intent == GENERAL_ASSISTANCE:
        intent = SEARCH_EXPERIENCES
        confidence = max(confidence, 0.72)

    return Classification(
        intent=intent,
        confidence=round(confidence, 2),
        entities={"query": cleaned},
        constraints=constraints,
        time_window=time_window,
        requires_tools=intent != GENERAL_ASSISTANCE or bool(constraints),
    )

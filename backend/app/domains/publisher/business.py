"""What kind of business a publisher is.

Mado's publisher model already carried the distinction that matters most - a
person (`individual`) or an organization (`organization`) - and nothing more.
That was right while a publisher was somebody posting an event. It stops being
enough once a hotel has a profile, because "hotel" and "museum" answer different
questions for an explorer and need different things on the page.

**This is a label on the existing Publisher, not a new entity.** The
implementation README lists Hotel, Resort, Restaurant, Cafe, Bar, Museum, Gym,
Spa, Venue, Attraction, Shopping Centre and Organization, and it would be easy to
read that as twelve models or twelve services. It is one column. Everything that
differs between a hotel and a cafe - where it is, what it offers, when it is open,
what it is suitable for - is already modelled: `catalog.venues` holds the
location and opening hours, `catalog.experiences` holds what they put on, and
`suitability` holds the claims. A `HotelService` would duplicate all three.

`industry` stays what it always was: free prose a publisher wrote about
themselves. This is the queryable fact.
"""

from __future__ import annotations

HOTEL = "hotel"
RESORT = "resort"
GUESTHOUSE = "guesthouse"
RESTAURANT = "restaurant"
CAFE = "cafe"
BAR = "bar"
MUSEUM = "museum"
GALLERY = "gallery"
VENUE = "venue"
ATTRACTION = "attraction"
GYM = "gym"
SPA = "spa"
SHOPPING = "shopping"
TOUR_OPERATOR = "tour_operator"
ORGANIZATION = "organization"
OTHER = "other"

# Somewhere you can stay. Grouped because "where can I stay this weekend" is one
# question and answering it with a gym would be absurd - and because the explorer
# page for these wants to lead with rooms rather than with tonight's programme.
STAYS = frozenset({HOTEL, RESORT, GUESTHOUSE})

# Somewhere that primarily serves food or drink.
FOOD_AND_DRINK = frozenset({RESTAURANT, CAFE, BAR})

BUSINESS_TYPES: dict[str, str] = {
    HOTEL: "Hotel",
    RESORT: "Resort",
    GUESTHOUSE: "Guesthouse",
    RESTAURANT: "Restaurant",
    CAFE: "Cafe",
    BAR: "Bar",
    MUSEUM: "Museum",
    GALLERY: "Gallery",
    VENUE: "Venue",
    ATTRACTION: "Attraction",
    GYM: "Gym",
    SPA: "Spa",
    SHOPPING: "Shopping centre",
    TOUR_OPERATOR: "Tour operator",
    ORGANIZATION: "Organization",
    OTHER: "Other",
}

ALL = frozenset(BUSINESS_TYPES)

# Social links a business may list. A fixed set rather than free-form keys,
# because the client renders an icon per platform and an unknown key would
# render as nothing at all - which looks like the link was lost rather than
# never understood.
SOCIAL_PLATFORMS = ("facebook", "instagram", "x", "tiktok", "youtube", "linkedin", "telegram")

MAX_SOCIAL_URL = 500


def label(business_type: str | None) -> str:
    """What this kind of business is called in a sentence."""
    if not business_type:
        return "Business"
    return BUSINESS_TYPES.get(business_type, business_type.replace("_", " ").capitalize())


def normalise_type(value: str | None) -> str | None:
    """Clean an incoming business type, or None.

    An unrecognised type becomes None rather than raising: it is a label, and
    refusing to save somebody's whole profile because of it would be a poor
    trade. None reads as "not stated", which is exactly true.
    """
    if not value:
        return None
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    return cleaned if cleaned in ALL else None


def normalise_social(value: dict | None) -> dict[str, str]:
    """Keep the platforms that are known and the values that look like links.

    Unknown platforms are dropped for the reason in SOCIAL_PLATFORMS. Values are
    length-bounded and required to be http(s), because these are rendered as
    anchors and a `javascript:` URL in a business profile is stored XSS waiting
    for somebody to click it.
    """
    if not isinstance(value, dict):
        return {}

    cleaned: dict[str, str] = {}
    for platform in SOCIAL_PLATFORMS:
        raw = value.get(platform)
        if not isinstance(raw, str):
            continue
        url = raw.strip()
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        cleaned[platform] = url[:MAX_SOCIAL_URL]
    return cleaned


__all__ = [
    "ALL",
    "BUSINESS_TYPES",
    "FOOD_AND_DRINK",
    "SOCIAL_PLATFORMS",
    "STAYS",
    "label",
    "normalise_social",
    "normalise_type",
]

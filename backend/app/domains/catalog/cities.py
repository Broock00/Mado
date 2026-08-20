"""Turning a point on the earth into the city row a venue can point at.

**This is not a table of places, and must not become one.** The rule in
`app/integrations/places.py` still holds: geography is resolved when somebody
asks and never curated. What a `City` row is, and all it is, is a label plus the
target of a foreign key - `catalog.venues.city_id` and
`catalog.experiences.city_id` are both NOT NULL, and the concierge reads
`city.timezone` to know when "tonight" is.

What changes here is the direction. A city used to be a **precondition**: a
developer typed one in, and until they had, nobody could add a venue there. That
is why this platform could be used in exactly ten cities, and why the composer
offered a dropdown of them. Now the row is a **consequence** - somebody drops a
pin, and whatever city that point turns out to be in is materialised on demand.

Three consequences worth knowing:

* **The coordinates decide, not the client.** The city is reverse-geocoded from
  the venue's own position server-side. Letting the client name it would let two
  venues on the same street file themselves under different cities, and would
  make a stored row depend on a value nobody checked.
* **`is_live` stays false.** That flag means the city has passed the launch
  checklist in spec BUSINESS-08 - a real editorial judgement about coverage,
  payments and moderation - and materialising a row is not that judgement.
  Nothing here ever sets it.
* **Rows are found before they are created.** Keyed on country plus name, so the
  eleventh venue in Nairobi joins the Nairobi row rather than making a second
  one.
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domains.catalog.localisation import currency_for_country
from app.domains.catalog.models import City
from app.integrations import places as places_module
from app.integrations import timezones as timezones_module
from app.integrations.places import Place

logger = get_logger("mado.cities")

# What a city is called when the provider names no locality - a venue in open
# country, on an island, or in a place whose administrative chain skips the level
# a city would occupy. Falling back through the chain rather than refusing keeps
# a rural venue postable, which is most of the point of removing the dropdown.
_NAME_ORDER = ("locality", "district", "county", "region", "country")


def _slugify(value: str, *, max_length: int = 100) -> str:
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")[:max_length].strip("-")


def city_name_from(place: Place) -> str | None:
    """The most city-like name in a resolved place's administrative chain."""
    for field in _NAME_ORDER:
        value = getattr(place, field, None)
        if value:
            return str(value)
    return None


async def _unique_slug(session: AsyncSession, base: str, country_code: str) -> str:
    """A free slug for a new city.

    Suffixed with the country when the bare name is taken, because the common
    collision here is a real one rather than a coincidence: there is a Paris in
    France and a Paris in Texas, and both are cities somebody may post in.
    """
    candidate = base or country_code.lower() or "city"
    for attempt in range(50):
        if attempt == 1 and country_code:
            candidate = f"{base}-{country_code.lower()}"
        elif attempt > 1:
            candidate = f"{base}-{country_code.lower()}-{attempt}"
        taken = await session.scalar(
            select(func.count()).select_from(City).where(City.slug == candidate)
        )
        if not taken:
            return candidate
    # Fifty cities sharing one name in one country is not a real situation, but
    # returning a duplicate slug would violate a unique constraint at flush time
    # and lose the whole post.
    raise RuntimeError(f"Could not find a free slug for city '{base}'")


async def city_for_place(session: AsyncSession, place: Place) -> City | None:
    """Find or create the city row a resolved place belongs to.

    Returns None only when the place carries no name at any administrative
    level, which in practice means the provider resolved nothing.
    """
    name = city_name_from(place)
    if not name:
        return None

    country_code = (place.country_code or "").upper()

    # Matched case-insensitively on name plus country. Name alone would merge
    # every Springfield on earth; the pair is what a person means by "the same
    # city", and the country code is the part providers are consistent about.
    existing = await session.scalar(
        select(City).where(
            func.lower(City.name) == name.lower(),
            City.country_code == country_code,
        )
    )
    if existing is not None:
        return existing

    zone = await timezones_module.get_provider().zone_for(
        place.latitude, place.longitude, country_code=country_code or None
    )
    if zone is None:
        logger.warning(
            "city_timezone_unresolved",
            city=name,
            country=country_code,
            # Worth a warning rather than an info: everything the concierge says
            # about "tonight" in this city will be in UTC until it is corrected.
            detail="falling back to UTC; times in this city will be wrong",
        )

    city = City(
        name=name,
        slug=await _unique_slug(session, _slugify(name), country_code),
        country=place.country or name,
        country_code=country_code or "ZZ",
        timezone=zone or timezones_module.FALLBACK_ZONE,
        currency=currency_for_country(country_code),
        languages=[],
        # The place's own coordinates, which for a locality result is the city
        # centre and for a venue result is the venue. Close enough for a label:
        # nothing scopes discovery by this - an `Area` does that - so a centre
        # that is a few streets out costs nothing.
        latitude=place.latitude,
        longitude=place.longitude,
        # Never true from here. Going live is an editorial decision, not a
        # side effect of somebody adding a venue.
        is_live=False,
    )
    session.add(city)
    await session.flush()
    logger.info(
        "city_materialised",
        city=city.slug,
        country=city.country_code,
        timezone=city.timezone,
        currency=city.currency,
    )
    return city


async def city_for_point(session: AsyncSession, latitude: float, longitude: float) -> City | None:
    """The city at these coordinates, created if it is new.

    The reverse geocode is cached inside the place provider on a coordinate key
    rounded to about a hundred metres, so a publisher whose map picker already
    resolved this point usually pays nothing for this call.
    """
    place = await places_module.get_provider().describe(latitude, longitude)
    if place is None:
        return None
    return await city_for_place(session, place)

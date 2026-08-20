"""Catalog queries.

Centralising the loader options matters more than it looks: an experience card
needs venue, neighbourhood, category, tags, media and publisher, and letting each
route assemble its own query is how N+1 problems get in (spec 80.02 s18).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.domains.catalog import suitability
from app.domains.catalog.models import (
    MODERATION_APPROVED,
    MODERATION_PENDING,
    STATUS_PUBLISHED,
    Category,
    City,
    EventInstance,
    Experience,
    Tag,
    Venue,
)

# Pending content stays visible while it waits for a human: spec BUSINESS-07 treats
# automated suspicion as a reason to look, not a reason to hide. Flagged and
# rejected are withheld.
DISCOVERABLE_MODERATION_STATUSES = (MODERATION_APPROVED, MODERATION_PENDING)


def with_card_relations(stmt: Select) -> Select:
    """Eager-load everything `to_summary` touches.

    Public because any query feeding a card needs exactly this set, and a caller
    that assembles its own inevitably misses one - `venue.neighborhood` is the
    usual casualty. The miss is not a slow query under asyncio, it is a
    MissingGreenlet at request time with no application frame in the traceback.
    """
    return stmt.options(
        selectinload(Experience.venue).selectinload(Venue.neighborhood),
        selectinload(Experience.category),
        selectinload(Experience.tags),
        selectinload(Experience.media),
        selectinload(Experience.publisher),
        selectinload(Experience.city),
        selectinload(Experience.events),
    )


def published_experiences() -> Select:
    """Base query for anything an explorer is allowed to see.

    Two independent gates, and both must pass: the author published it, and
    moderation has not withheld it. Every discovery surface builds on this
    query, so an item flagged or rejected disappears from the feed, search,
    nearby and concierge answers at once - there is no path that forgets to
    check.
    """
    return with_card_relations(
        select(Experience).where(
            Experience.status == STATUS_PUBLISHED,
            Experience.deleted_at.is_(None),
            Experience.moderation_status.in_(DISCOVERABLE_MODERATION_STATUSES),
        )
    )


async def get_city_by_slug(session: AsyncSession, slug: str) -> City | None:
    result = await session.execute(select(City).where(City.slug == slug))
    return result.scalar_one_or_none()


async def list_cities(session: AsyncSession, *, live_only: bool = False) -> list[City]:
    stmt = select(City).order_by(City.name)
    if live_only:
        stmt = stmt.where(City.is_live.is_(True))
    result = await session.execute(stmt)
    return list(result.scalars().all())


# How far from a city somebody can be and still be considered "in" it. Generous
# on purpose: an explorer in a suburb, on a ring road or at the airport is still
# looking for that city's evening. Beyond it they are somewhere Mado does not
# cover yet, and saying so is better than quietly showing them a city they are
# nowhere near.
NEAREST_CITY_MAX_KM = 120.0

# Beyond this, a bounding box has stopped describing a place. The United States
# reports 360 degrees of longitude because its territories cross the
# antimeridian, and a geography polygon that wide intersects nothing useful.
MAX_BOX_DEGREES = 170.0

EARTH_RADIUS_KM = 6371.0088


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Haversine, because these are city centres and a
    flat-earth approximation is wrong by tens of kilometres at high latitudes."""
    from math import asin, cos, radians, sin, sqrt

    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def bounding_box(latitude: float, max_km: float) -> tuple[float, float]:
    """Degree spans that certainly contain everything within `max_km`.

    One degree of latitude is ~111 km everywhere. Longitude narrows towards the
    poles - about 48 km per degree in Reykjavik - so the longitude span is
    widened by 1/cos(lat). Without that correction a search up there misses
    cities well inside the radius, which is a bug that only ever shows up far
    from where it was written.

    Deliberately generous: the box only has to avoid false negatives, because
    the exact haversine afterwards removes anything it lets through.
    """
    from math import cos, radians

    lat_span = max_km / 111.0
    # Clamped so the poles do not divide by zero and produce an infinite span.
    shrink = max(cos(radians(latitude)), 0.01)
    return lat_span, lat_span / shrink


async def nearest_live_city(
    session: AsyncSession,
    latitude: float,
    longitude: float,
    *,
    max_km: float = NEAREST_CITY_MAX_KM,
) -> tuple[City, float] | None:
    """The live city somebody is actually in, or None if Mado does not cover it.

    Returning None matters more than the happy path. The alternative - falling
    back to a default city - is what made every explorer on the platform look
    like they were in Addis Ababa regardless of where they opened the app, and
    a wrong answer delivered confidently is worse than an honest empty one.

    A bounding box in SQL first, then an exact distance in Python over what
    survives. The box is cheap and indexable and throws away almost everything;
    doing haversine in SQL over every city on earth would not be.
    """
    lat_span, lon_span = bounding_box(latitude, max_km)

    stmt = select(City).where(
        City.is_live.is_(True),
        City.latitude.between(latitude - lat_span, latitude + lat_span),
        City.longitude.between(longitude - lon_span, longitude + lon_span),
    )
    candidates = list((await session.execute(stmt)).scalars().all())
    if not candidates:
        return None

    ranked = sorted(
        (
            (city, distance_km(latitude, longitude, city.latitude, city.longitude))
            for city in candidates
        ),
        key=lambda pair: pair[1],
    )
    city, distance = ranked[0]
    return (city, distance) if distance <= max_km else None


# How a request's city was arrived at, reported back so an interface can say
# "near you" or "showing Paris" honestly - and so "we do not know where you are"
# is a state it can respond to instead of a default it cannot see.
RESOLVED_BY_CHOSEN = "chosen"
RESOLVED_BY_LOCATION = "location"
RESOLVED_BY_UNKNOWN = "unknown"


async def resolve_city_slug(
    session: AsyncSession,
    *,
    city: str | None,
    latitude: float | None,
    longitude: float | None,
) -> tuple[str | None, str]:
    """Which city a request is about, and how that was decided.

    One implementation for discovery, the concierge and the planner, because
    three copies of "which city is this" is three chances to leave one of them
    defaulting to somewhere the explorer has never been.

    An explicit choice always wins: planning a trip to a city you are not in yet
    is an ordinary thing to do. Otherwise coordinates decide. When there is
    neither, the answer is None and the caller asks rather than guesses.
    """
    if city:
        return city, RESOLVED_BY_CHOSEN
    if latitude is not None and longitude is not None:
        found = await nearest_live_city(session, latitude, longitude)
        if found is not None:
            return found[0].slug, RESOLVED_BY_LOCATION
    return None, RESOLVED_BY_UNKNOWN


async def list_categories(session: AsyncSession) -> list[Category]:
    result = await session.execute(select(Category).order_by(Category.sort_order, Category.name))
    return list(result.scalars().all())


async def get_experience(session: AsyncSession, experience_id: uuid.UUID) -> Experience | None:
    result = await session.execute(published_experiences().where(Experience.id == experience_id))
    return result.scalar_one_or_none()


async def get_experience_by_slug(session: AsyncSession, slug: str) -> Experience | None:
    result = await session.execute(published_experiences().where(Experience.slug == slug))
    return result.scalar_one_or_none()


async def get_experiences_by_ids(session: AsyncSession, ids: list[uuid.UUID]) -> list[Experience]:
    """Fetch a set of experiences, preserving the caller's ordering.

    Search returns candidates already ordered by the ranking layer; a plain IN query
    would silently reorder them by physical row order.
    """
    if not ids:
        return []
    result = await session.execute(published_experiences().where(Experience.id.in_(ids)))
    found = {exp.id: exp for exp in result.scalars().all()}
    return [found[i] for i in ids if i in found]


@dataclass(frozen=True, slots=True)
class Area:
    """Where a query is about: a point with a radius, or a named city.

    The point is the real one. A city slug survives because an explorer can
    still choose a city explicitly and because the seeded catalogue is organised
    that way, but geography is not Mado's data and a curated row must never be
    the only way to ask "what is near here". Everything the platform learns
    about the world arrives as coordinates.

    Nothing set means everywhere, which is what search wants and what a feed
    never does.
    """

    latitude: float | None = None
    longitude: float | None = None
    radius_km: float | None = None
    # South, west, north, east. Present when the place has real extent, which a
    # radius cannot represent: the centre of Kenya is four hundred kilometres
    # from Nairobi and the centre of the United States is in Kansas, so a radius
    # around either finds nothing at all.
    bounding_box: tuple[float, float, float, float] | None = None
    # An ISO country code, used when the explorer picked a whole country. Exact
    # where a box is not: a bounding box around Kenya also covers parts of four
    # neighbours, and the one around the United States spans 360 degrees of
    # longitude because of the Pacific territories - which is not a polygon any
    # geography library can intersect usefully.
    country_code: str | None = None
    city_slug: str | None = None

    @property
    def has_box(self) -> bool:
        if self.bounding_box is None:
            return False
        south, west, north, east = self.bounding_box
        # A box this wide is not describing a place, it is describing a
        # projection artefact. Treated as absent so the caller falls back to
        # something meaningful.
        return (east - west) < MAX_BOX_DEGREES and (north - south) < MAX_BOX_DEGREES

    @property
    def has_point(self) -> bool:
        return (
            self.latitude is not None
            and self.longitude is not None
            and (self.radius_km or 0) > 0
        )

    @property
    def is_everywhere(self) -> bool:
        return (
            not self.has_point
            and not self.has_box
            and not self.country_code
            and not self.city_slug
        )

    def widened(self, radius_km: float) -> Area:
        return Area(
            latitude=self.latitude,
            longitude=self.longitude,
            radius_km=radius_km,
            bounding_box=self.bounding_box,
            country_code=self.country_code,
            city_slug=self.city_slug,
        )

    def ladder(self) -> list[Area]:
        """The same place, looked at from close in and then further out.

        "What is happening near you" should mean the next street before it means
        the far side of the city, and an empty answer at two kilometres is not
        an empty city. Only a point widens - a chosen city is already the area
        the explorer asked for.
        """
        # A box or a country already describes exactly the area asked for.
        # Widening either would be widening it into the sea.
        if self.has_box or self.country_code or not self.has_point:
            return [self]
        start = self.radius_km or RADIUS_STEPS_KM[0]
        steps = [step for step in RADIUS_STEPS_KM if step > start]
        return [self] + [self.widened(step) for step in steps]


# How "near you" widens when there is not much close by. Walking distance, then
# the neighbourhood, then across town, then the whole metropolitan area.
RADIUS_STEPS_KM = (2.0, 5.0, 15.0, 40.0)

# Below this many candidates, the next radius is tried. Not a target - the
# ranker still decides what is worth showing - but a pool this thin cannot be
# ranked into anything good.
MIN_CANDIDATES = 12


def scope_to_area(stmt: Select, area: Area | None) -> Select:
    """Restrict a query to an area.

    A radius is a PostGIS `ST_DWithin` against the venue's geography column,
    which is indexed. It therefore only finds experiences that have a venue -
    correct, because something with no location cannot be near anybody, and
    worth knowing when a listing does not appear in a nearby rail.
    """
    if area is None or area.is_everywhere:
        return stmt
    if area.country_code:
        # Exact, and cheaper than geometry. A venue belongs to a city and a city
        # knows its country, so this needs no coordinates at all.
        return stmt.join(City, Experience.city_id == City.id).where(
            City.country_code == area.country_code.upper()
        )
    if area.has_box:
        # An envelope, not a radius. `ST_MakeEnvelope` takes west, south, east,
        # north - a different order from the one geocoders report, and getting
        # it wrong silently searches a box on the other side of the equator.
        south, west, north, east = area.bounding_box
        # Cast to a POLYGON geography, not to the column's own type - that is a
        # POINT, and Postgres rejects an envelope cast to it outright. Casting
        # the envelope rather than the column keeps the GiST index usable.
        envelope = func.ST_MakeEnvelope(west, south, east, north, 4326).cast(
            Geography(geometry_type="POLYGON", srid=4326)
        )
        return stmt.join(Venue, Experience.venue_id == Venue.id).where(
            func.ST_Intersects(Venue.geo, envelope)
        )
    if area.has_point:
        point = func.ST_SetSRID(
            func.ST_MakePoint(area.longitude, area.latitude), 4326
        ).cast(Venue.geo.type)
        return stmt.join(Venue, Experience.venue_id == Venue.id).where(
            func.ST_DWithin(Venue.geo, point, (area.radius_km or 0) * 1000)
        )
    return stmt.join(City, Experience.city_id == City.id).where(City.slug == area.city_slug)


def require_suitability(stmt: Select, required: list[str] | None) -> Select:
    """Keep only listings that have actually claimed everything in `required`.

    Each requirement is satisfied by the experience's own claims **or** its
    venue's, so "somewhere with a play area" finds a puppet show in a building
    that has one. That is an OR across two columns per requirement, ANDed across
    requirements - which is why this is built as a loop rather than one array
    containment check.

    The join to `Venue` is an outer join, and it has to be. An inner join would
    silently drop every listing without a venue, so a requirement the experience
    itself satisfies would exclude it for having no building - and a walking tour
    has no building.

    **Unknown is excluded here, deliberately.** A listing that has claimed
    nothing is not returned for a requirement, because this function is only ever
    called for constraints the explorer stated as requirements rather than
    preferences. Somewhere that might be step-free is not an answer to somebody
    who cannot use stairs; ranking them lower is not enough, because the top of a
    thin result is still the top.
    """
    wanted = suitability.normalise(required)
    if not wanted:
        return stmt

    # Aliased so this composes with a caller that has already joined Venue for
    # geography - `scope_to_area` does exactly that for a box or a radius, and a
    # second unaliased join to the same table is a SQL error.
    venue = aliased(Venue)
    stmt = stmt.outerjoin(venue, Experience.venue_id == venue.id)

    for slug in wanted:
        # Anything that implies the requirement satisfies it: a vegan kitchen
        # answers a request for vegetarian food, and a query that did not know
        # this would discard the best match it had.
        satisfying = sorted(suitability.expand([slug]))
        stmt = stmt.where(
            or_(
                Experience.suitability.overlap(satisfying),
                venue.facilities.overlap(satisfying),
            )
        )
    return stmt


async def query_experiences(
    session: AsyncSession,
    *,
    area: Area | None = None,
    category_slugs: list[str] | None = None,
    tag_slugs: list[str] | None = None,
    experience_type: str | None = None,
    free_only: bool = False,
    indoor: bool | None = None,
    required_suitability: list[str] | None = None,
    starts_between: tuple[datetime, datetime] | None = None,
    limit: int = 60,
) -> list[Experience]:
    """Filtered catalog read used by the feed modules and as the search fallback."""
    stmt = scope_to_area(published_experiences(), area)

    if category_slugs:
        stmt = stmt.join(Category, Experience.category_id == Category.id).where(
            Category.slug.in_(category_slugs)
        )
    if tag_slugs:
        stmt = stmt.where(Experience.tags.any(Tag.slug.in_(tag_slugs)))
    if experience_type:
        stmt = stmt.where(Experience.type == experience_type)
    if free_only:
        stmt = stmt.where(Experience.price_type == "free")
    if indoor is not None:
        stmt = stmt.where(Experience.is_indoor.is_(indoor))

    stmt = require_suitability(stmt, required_suitability)

    if starts_between is not None:
        start, end = starts_between
        # Restrict to experiences with at least one live occurrence in the window.
        stmt = stmt.where(
            Experience.events.any(
                and_(
                    EventInstance.start_time >= start,
                    EventInstance.start_time <= end,
                    EventInstance.status != "cancelled",
                )
            )
        )

    stmt = stmt.order_by(
        Experience.popularity_score.desc(), Experience.published_at.desc().nullslast()
    ).limit(limit)

    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def nearby_experiences(
    session: AsyncSession,
    *,
    latitude: float,
    longitude: float,
    radius_km: float = 5.0,
    limit: int = 60,
) -> list[Experience]:
    """Radius search using the PostGIS geography column.

    ``ST_DWithin`` on a geography type takes metres and uses the GiST index, so this
    stays an index scan rather than a full-table distance computation.
    """
    point = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326).cast(Venue.geo.type)
    stmt = (
        published_experiences()
        .join(Venue, Experience.venue_id == Venue.id)
        .where(func.ST_DWithin(Venue.geo, point, radius_km * 1000))
        .order_by(func.ST_Distance(Venue.geo, point))
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def upcoming_events(
    session: AsyncSession,
    *,
    experience_id: uuid.UUID | None = None,
    area: Area | None = None,
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
    limit: int = 50,
) -> list[EventInstance]:
    stmt = (
        select(EventInstance)
        .join(Experience, EventInstance.experience_id == Experience.id)
        .where(
            Experience.status == STATUS_PUBLISHED,
            Experience.deleted_at.is_(None),
            Experience.moderation_status.in_(DISCOVERABLE_MODERATION_STATUSES),
            EventInstance.status != "cancelled",
        )
        .options(
            selectinload(EventInstance.experience)
            .selectinload(Experience.venue)
            .selectinload(Venue.neighborhood),
            selectinload(EventInstance.experience).selectinload(Experience.category),
            selectinload(EventInstance.experience).selectinload(Experience.media),
            selectinload(EventInstance.experience).selectinload(Experience.publisher),
            selectinload(EventInstance.experience).selectinload(Experience.tags),
            selectinload(EventInstance.experience).selectinload(Experience.city),
        )
        .order_by(EventInstance.start_time)
        .limit(limit)
    )
    if experience_id is not None:
        stmt = stmt.where(EventInstance.experience_id == experience_id)
    stmt = scope_to_area(stmt, area)
    if starts_after is not None:
        stmt = stmt.where(EventInstance.start_time >= starts_after)
    if starts_before is not None:
        stmt = stmt.where(EventInstance.start_time <= starts_before)

    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def get_event(session: AsyncSession, event_id: uuid.UUID) -> EventInstance | None:
    result = await session.execute(
        select(EventInstance)
        .where(EventInstance.id == event_id)
        .options(
            selectinload(EventInstance.experience)
            .selectinload(Experience.venue)
            .selectinload(Venue.neighborhood),
            selectinload(EventInstance.experience).selectinload(Experience.category),
            selectinload(EventInstance.experience).selectinload(Experience.media),
            selectinload(EventInstance.experience).selectinload(Experience.publisher),
            selectinload(EventInstance.experience).selectinload(Experience.tags),
            selectinload(EventInstance.experience).selectinload(Experience.city),
        )
    )
    return result.scalar_one_or_none()


async def similar_experiences(
    session: AsyncSession, experience: Experience, *, limit: int = 12
) -> list[Experience]:
    """Related items by shared category or tags, excluding the source.

    Uses the relational graph rather than embeddings so this works before the
    embedding backfill has run; the vector path replaces it once populated.
    """
    tag_ids = [tag.id for tag in (experience.tags or [])]
    conditions = []
    if experience.category_id is not None:
        conditions.append(Experience.category_id == experience.category_id)
    if tag_ids:
        conditions.append(Experience.tags.any(Tag.id.in_(tag_ids)))
    if not conditions:
        conditions.append(Experience.city_id == experience.city_id)

    stmt = (
        published_experiences()
        .where(or_(*conditions))
        .where(Experience.id != experience.id)
        .order_by(Experience.quality_score.desc(), Experience.popularity_score.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())

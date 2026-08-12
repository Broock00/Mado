"""Invented listings in real places, so the worldwide behaviour can be seen.

The platform resolves geography from OpenStreetMap and scopes discovery by a
point and a radius, which means it works anywhere. The catalogue does not: every
real listing is in Addis Ababa, so an explorer in New York gets a correct and
completely empty page. This fills a few places with plausible content so the
behaviour is visible and testable now.

**This data is not real and must never reach explorers.** A made-up bar is worse
than an empty city: somebody travels to it. Three things enforce that rather
than relying on anybody remembering.

* It refuses to run when the environment is production.
* Every row it writes is marked - publishers are slugged `demo-`, experiences
  carry ``attributes["demo"] = True`` - so nothing here can quietly become
  indistinguishable from a real listing.
* ``remove()`` deletes exactly what it created, found by those marks.

**The coordinates are real.** That is the entire point: the radius search, the
distance ranking and the reverse geocoding are what this exists to exercise, and
invented coordinates would exercise none of them. Every venue below sits within
a few hundred metres of the real place it is named after.

City rows are created for these places. That does not contradict geography
living outside the database - a `City` is a label and the thing a venue's
foreign key points at, not the scoping key it used to be. Discovery finds these
listings by coordinates, and would find them with no city row at all if the
schema let a venue exist without one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domains.catalog.models import (
    MODERATION_APPROVED,
    STATUS_PUBLISHED,
    TYPE_EVENT,
    TYPE_PLACE,
    Category,
    City,
    EventInstance,
    Experience,
    Neighborhood,
    Venue,
)
from app.domains.publisher.models import TYPE_ORGANIZATION, Publisher

logger = get_logger("mado.seed.demo")

# The mark. One string, used for the publisher slug prefix, the attribute on
# every experience, and the query that removes all of it.
DEMO_MARK = "demo"


@dataclass(slots=True)
class DemoVenue:
    name: str
    address: str
    latitude: float
    longitude: float
    neighbourhood: str


@dataclass(slots=True)
class DemoListing:
    title: str
    venue: str
    category: str
    summary: str
    description: str
    kind: str = TYPE_PLACE
    price_type: str = "free"
    price_amount: float | None = None
    duration_minutes: int | None = None
    is_indoor: bool | None = None
    rating: float | None = None
    rating_count: int = 0
    # Hours from now that occurrences start. Empty for a place, which is open
    # rather than scheduled.
    occurrences: tuple[int, ...] = ()


@dataclass(slots=True)
class DemoCity:
    name: str
    slug: str
    country: str
    country_code: str
    timezone: str
    currency: str
    languages: list[str]
    latitude: float
    longitude: float
    neighbourhoods: list[tuple[str, float, float]]
    venues: list[DemoVenue] = field(default_factory=list)
    listings: list[DemoListing] = field(default_factory=list)


# Real coordinates throughout. Checked against a map: each venue is within a few
# hundred metres of the place it borrows its name from, so distances and radius
# searches behave the way they will in production.
CITIES: list[DemoCity] = [
    DemoCity(
        name="New York",
        slug="new-york",
        country="United States",
        country_code="US",
        timezone="America/New_York",
        currency="USD",
        languages=["en", "es"],
        latitude=40.7128,
        longitude=-74.0060,
        neighbourhoods=[
            ("Williamsburg", 40.7081, -73.9571),
            ("East Village", 40.7265, -73.9815),
            ("Lower Manhattan", 40.7075, -74.0113),
        ],
        venues=[
            DemoVenue("Wythe Rooftop", "80 Wythe Ave, Brooklyn", 40.7220, -73.9578, "Williamsburg"),
            DemoVenue("Domino Park", "300 Kent Ave, Brooklyn", 40.7143, -73.9682, "Williamsburg"),
            DemoVenue("Tompkins Hall", "500 E 9th St", 40.7275, -73.9800, "East Village"),
            DemoVenue("Seaport Studio", "19 Fulton St", 40.7062, -74.0030, "Lower Manhattan"),
        ],
        listings=[
            DemoListing(
                title="Sunset Sets on the Wythe Roof",
                venue="Wythe Rooftop",
                category="nightlife",
                summary="Vinyl-only sets as the light goes off the Manhattan skyline.",
                description=(
                    "A small rooftop that fills early on clear evenings. Two residents "
                    "trade forty-minute sets from seven, mostly disco and Brazilian "
                    "records, and the bar stops serving at eleven because of the "
                    "neighbours. Go up the stairs by the loading bay rather than the "
                    "lobby lift, which is slower than it looks."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=25,
                duration_minutes=240,
                is_indoor=False,
                rating=4.5,
                rating_count=182,
                occurrences=(28, 52, 100),
            ),
            DemoListing(
                title="Sunday Market at Domino Park",
                venue="Domino Park",
                category="food-drink",
                summary="Forty stalls under the old refinery cranes, by the water.",
                description=(
                    "Produce at the north end, cooked food in the middle, and a row of "
                    "bakers who sell out before noon. The park itself is worth the walk "
                    "even when the market is not on - the elevated walkway gives you the "
                    "bridge from an angle you cannot get at street level."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=180,
                is_indoor=False,
                rating=4.3,
                rating_count=96,
                occurrences=(76, 244),
            ),
            DemoListing(
                title="Basement Jazz at Tompkins Hall",
                venue="Tompkins Hall",
                category="music",
                summary="A twenty-eight seat room with a piano nobody has replaced since 1974.",
                description=(
                    "Two sets a night, the second usually looser than the first. Cash at "
                    "the door, no card machine, and the room is small enough that talking "
                    "through a solo will be noticed. Arrive for the first set if you want "
                    "to sit down."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=20,
                duration_minutes=150,
                is_indoor=True,
                rating=4.7,
                rating_count=311,
                occurrences=(6, 30, 54),
            ),
            DemoListing(
                title="Seaport Print Studio",
                venue="Seaport Studio",
                category="arts-culture",
                summary="Open-access screenprinting, walk in and use a bench by the hour.",
                description=(
                    "Four bays, a drying rack that is always full, and someone on hand who "
                    "will show you how to pull a print without ruining the screen. Bring "
                    "your own artwork on acetate or use the cutter upstairs."
                ),
                price_type="fixed",
                price_amount=18,
                duration_minutes=120,
                is_indoor=True,
                rating=4.4,
                rating_count=58,
            ),
        ],
    ),
    DemoCity(
        name="London",
        slug="london",
        country="United Kingdom",
        country_code="GB",
        timezone="Europe/London",
        currency="GBP",
        languages=["en"],
        latitude=51.5072,
        longitude=-0.1276,
        neighbourhoods=[
            ("Peckham", 51.4739, -0.0691),
            ("Hackney", 51.5450, -0.0553),
            ("South Bank", 51.5060, -0.1150),
        ],
        venues=[
            DemoVenue("Rye Lane Arches", "133 Rye Ln, Peckham", 51.4712, -0.0699, "Peckham"),
            DemoVenue("Mare Street Works", "200 Mare St, Hackney", 51.5399, -0.0554, "Hackney"),
            DemoVenue(
                "Riverside Terrace", "Belvedere Rd, South Bank", 51.5062, -0.1160, "South Bank"
            ),
        ],
        listings=[
            DemoListing(
                title="Friday Night Under the Arches",
                venue="Rye Lane Arches",
                category="nightlife",
                summary="Two railway arches, a soundsystem, and trains overhead until midnight.",
                description=(
                    "The bar is in the first arch and the music is in the second, which "
                    "means you can actually hear someone speak. Doors at nine, busy by "
                    "eleven, and the queue moves faster than it looks because they let "
                    "people through in groups."
                ),
                kind=TYPE_EVENT,
                price_type="range",
                price_amount=8,
                duration_minutes=300,
                is_indoor=True,
                rating=4.2,
                rating_count=204,
                occurrences=(44, 212),
            ),
            DemoListing(
                title="Hackney Ceramics Evening Class",
                venue="Mare Street Works",
                category="learning",
                summary="Six weeks on the wheel, everything fired and glazed for you.",
                description=(
                    "Small groups, mostly beginners, and the tutor will let you throw the "
                    "same bad bowl four times rather than take over. Aprons provided; wear "
                    "something you do not mind losing to slip."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=45,
                duration_minutes=150,
                is_indoor=True,
                rating=4.8,
                rating_count=87,
                occurrences=(20, 188),
            ),
            DemoListing(
                title="Riverside Terrace",
                venue="Riverside Terrace",
                category="food-drink",
                summary="Concrete steps, a view of the river, and no obligation to buy anything.",
                description=(
                    "One of the few places on the South Bank where you can sit for an hour "
                    "without spending money. The kiosk does decent coffee until six. Busy "
                    "at sunset in summer, empty and rather good in the rain."
                ),
                price_type="free",
                is_indoor=False,
                rating=4.1,
                rating_count=143,
            ),
        ],
    ),
    DemoCity(
        name="Nairobi",
        slug="nairobi",
        country="Kenya",
        country_code="KE",
        timezone="Africa/Nairobi",
        currency="KES",
        languages=["en", "sw"],
        latitude=-1.2864,
        longitude=36.8172,
        neighbourhoods=[
            ("Westlands", -1.2650, 36.8030),
            ("Karen", -1.3190, 36.7100),
            ("Central Business District", -1.2841, 36.8233),
        ],
        venues=[
            DemoVenue(
                "Westlands Yard", "Woodvale Grove, Westlands", -1.2665, 36.8036, "Westlands"
            ),
            DemoVenue("Karen Forest Edge", "Karen Rd", -1.3204, 36.7118, "Karen"),
            DemoVenue(
                "Kenyatta Avenue Hall",
                "Kenyatta Ave",
                -1.2845,
                36.8220,
                "Central Business District",
            ),
        ],
        listings=[
            DemoListing(
                title="Thursday Live Band at the Yard",
                venue="Westlands Yard",
                category="music",
                summary="Benga and rumba, outdoors, from eight until late.",
                description=(
                    "An open courtyard with a stage at one end and food stalls along the "
                    "wall. The band plays two long sets; between them a DJ keeps it going. "
                    "Cover charge at the gate, drinks are cheaper inside than the street "
                    "bars either side."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=500,
                duration_minutes=240,
                is_indoor=False,
                rating=4.6,
                rating_count=167,
                occurrences=(34, 202),
            ),
            DemoListing(
                title="Forest Walk at Karen Edge",
                venue="Karen Forest Edge",
                category="outdoors",
                summary="An hour of indigenous woodland, guided, early enough to hear it.",
                description=(
                    "Leaves at half six while the colobus are still moving. The guide is "
                    "worth having for the birds - you will miss most of them otherwise. "
                    "Sturdy shoes; the path is red earth and slick after rain."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=1200,
                duration_minutes=90,
                is_indoor=False,
                rating=4.7,
                rating_count=74,
                occurrences=(14, 38, 62),
            ),
            DemoListing(
                title="Kenyatta Avenue Hall",
                venue="Kenyatta Avenue Hall",
                category="arts-culture",
                summary="A working exhibition hall in the middle of town, free to enter.",
                description=(
                    "Whatever is on changes monthly and is usually worth twenty minutes. "
                    "The building itself is the draw: a 1960s civic interior that has been "
                    "left alone rather than modernised."
                ),
                price_type="free",
                is_indoor=True,
                rating=4.0,
                rating_count=52,
            ),
        ],
    ),
]


def _slug(city: DemoCity, text: str) -> str:
    base = "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")
    while "--" in base:
        base = base.replace("--", "-")
    return f"{DEMO_MARK}-{city.slug}-{base}"[:200]


async def _category_ids(session: AsyncSession) -> dict[str, uuid.UUID]:
    """Reuse whatever categories the real seed created.

    A listing with no category still ranks and still shows; inventing a parallel
    taxonomy for demo data would put two vocabularies in one catalogue.
    """
    rows = (await session.execute(select(Category))).scalars().all()
    return {row.slug: row.id for row in rows}


async def seed_demo_cities(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Create the demo places. Idempotent - re-running updates rather than doubles."""
    settings = get_settings()
    if settings.environment == "production":
        # Not a warning. Invented listings reaching real explorers means somebody
        # travels to a bar that does not exist.
        raise RuntimeError(
            "Demo cities must never be seeded in production - this data is invented."
        )

    now = now or datetime.now(UTC)
    categories = await _category_ids(session)
    counts = {"cities": 0, "venues": 0, "listings": 0, "occurrences": 0}

    for demo in CITIES:
        city = (
            await session.execute(select(City).where(City.slug == demo.slug))
        ).scalar_one_or_none()
        if city is None:
            city = City(slug=demo.slug)
            session.add(city)
        city.name = demo.name
        city.country = demo.country
        city.country_code = demo.country_code
        city.timezone = demo.timezone
        city.currency = demo.currency
        city.languages = demo.languages
        city.latitude = demo.latitude
        city.longitude = demo.longitude
        city.is_live = True
        await session.flush()
        counts["cities"] += 1

        neighbourhoods: dict[str, Neighborhood] = {}
        for name, latitude, longitude in demo.neighbourhoods:
            slug = _slug(demo, name)
            found = (
                await session.execute(select(Neighborhood).where(Neighborhood.slug == slug))
            ).scalar_one_or_none()
            if found is None:
                found = Neighborhood(slug=slug, city_id=city.id)
                session.add(found)
            found.name = name
            found.latitude = latitude
            found.longitude = longitude
            found.city_id = city.id
            neighbourhoods[name] = found
        await session.flush()

        publisher_slug = f"{DEMO_MARK}-{demo.slug}"
        publisher = (
            await session.execute(select(Publisher).where(Publisher.slug == publisher_slug))
        ).scalar_one_or_none()
        if publisher is None:
            publisher = Publisher(slug=publisher_slug)
            session.add(publisher)
        publisher.name = f"{demo.name} listings (demo)"
        publisher.type = TYPE_ORGANIZATION
        publisher.description = (
            "Sample data. These listings are invented so the platform can be "
            "exercised outside its pilot city; none of them is a real venue."
        )
        await session.flush()

        venues: dict[str, Venue] = {}
        for item in demo.venues:
            slug = _slug(demo, item.name)
            found = (
                await session.execute(select(Venue).where(Venue.slug == slug))
            ).scalar_one_or_none()
            if found is None:
                found = Venue(slug=slug)
                session.add(found)
            found.name = item.name
            found.city_id = city.id
            found.publisher_id = publisher.id
            found.neighborhood_id = neighbourhoods[item.neighbourhood].id
            found.address = item.address
            found.latitude = item.latitude
            found.longitude = item.longitude
            venues[item.name] = found
            counts["venues"] += 1
        await session.flush()

        for listing in demo.listings:
            slug = _slug(demo, listing.title)
            found = (
                await session.execute(select(Experience).where(Experience.slug == slug))
            ).scalar_one_or_none()
            if found is None:
                found = Experience(slug=slug)
                session.add(found)
            found.publisher_id = publisher.id
            found.city_id = city.id
            found.venue_id = venues[listing.venue].id
            found.category_id = categories.get(listing.category)
            found.title = listing.title
            found.summary = listing.summary
            found.description = listing.description
            found.type = listing.kind
            found.status = STATUS_PUBLISHED
            found.moderation_status = MODERATION_APPROVED
            found.published_at = now
            found.price_type = listing.price_type
            found.price_amount = listing.price_amount
            found.currency = demo.currency
            found.duration_minutes = listing.duration_minutes
            found.is_indoor = listing.is_indoor
            found.rating_average = listing.rating
            found.rating_count = listing.rating_count
            # The mark that makes this findable and removable, and that stops it
            # ever passing for a real listing.
            found.attributes = {**(found.attributes or {}), DEMO_MARK: True}
            await session.flush()
            counts["listings"] += 1

            if listing.occurrences:
                existing = {
                    occurrence.start_time.replace(tzinfo=UTC)
                    if occurrence.start_time.tzinfo is None
                    else occurrence.start_time: occurrence
                    for occurrence in (
                        await session.execute(
                            select(EventInstance).where(
                                EventInstance.experience_id == found.id
                            )
                        )
                    ).scalars()
                }
                local = ZoneInfo(demo.timezone)
                for hours in listing.occurrences:
                    # Snapped to a plausible evening in the city's own timezone
                    # rather than left at whatever time the seed happened to run.
                    start = (now + timedelta(hours=hours)).astimezone(local)
                    start = start.replace(minute=0, second=0, microsecond=0).astimezone(UTC)
                    if start in existing:
                        continue
                    session.add(
                        EventInstance(
                            experience_id=found.id,
                            start_time=start,
                            end_time=start + timedelta(minutes=listing.duration_minutes or 120),
                            status="scheduled",
                            capacity=60,
                            remaining=60,
                        )
                    )
                    counts["occurrences"] += 1

    await session.flush()
    logger.info("demo_cities_seeded", **counts)
    return counts


async def remove_demo_cities(session: AsyncSession) -> dict[str, int]:
    """Delete exactly what `seed_demo_cities` created.

    Found by the marks rather than by the list above, so a place removed from
    the data is still cleaned up from a database that already has it.
    """
    counts = {"listings": 0, "venues": 0, "cities": 0}

    publishers = (
        await session.execute(
            select(Publisher).where(Publisher.slug.like(f"{DEMO_MARK}-%"))
        )
    ).scalars().all()
    publisher_ids = [publisher.id for publisher in publishers]
    if not publisher_ids:
        return counts

    experiences = (
        await session.execute(
            select(Experience).where(Experience.publisher_id.in_(publisher_ids))
        )
    ).scalars().all()
    for experience in experiences:
        for occurrence in (
            await session.execute(
                select(EventInstance).where(EventInstance.experience_id == experience.id)
            )
        ).scalars():
            await session.delete(occurrence)
        await session.delete(experience)
        counts["listings"] += 1
    await session.flush()

    for venue in (
        await session.execute(select(Venue).where(Venue.slug.like(f"{DEMO_MARK}-%")))
    ).scalars():
        await session.delete(venue)
        counts["venues"] += 1
    for neighbourhood in (
        await session.execute(
            select(Neighborhood).where(Neighborhood.slug.like(f"{DEMO_MARK}-%"))
        )
    ).scalars():
        await session.delete(neighbourhood)
    for publisher in publishers:
        await session.delete(publisher)
    await session.flush()

    for demo in CITIES:
        city = (
            await session.execute(select(City).where(City.slug == demo.slug))
        ).scalar_one_or_none()
        if city is None:
            continue
        # Only if nothing real moved in while the demo data was there.
        remaining = (
            await session.execute(select(Experience).where(Experience.city_id == city.id))
        ).scalars().first()
        if remaining is None:
            await session.delete(city)
            counts["cities"] += 1

    await session.flush()
    logger.info("demo_cities_removed", **counts)
    return counts

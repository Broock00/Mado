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
from app.domains.catalog import suitability as suitability_vocab
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
    # What the building itself offers, over the vocabulary in
    # `catalog/suitability.py`. Left empty on most venues on purpose: a demo
    # catalogue where every venue claims everything would make a constrained
    # search look like it was doing nothing, because nothing would ever be
    # filtered out.
    facilities: tuple[str, ...] = ()


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
    # What this listing claims to be suitable for. Sparse deliberately - see the
    # note on DemoVenue.facilities.
    suitability: tuple[str, ...] = ()
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
            ("Greenpoint", 40.7304, -73.9540),
            ("Prospect Heights", 40.6774, -73.9668),
            ("Astoria", 40.7644, -73.9235),
            ("Harlem", 40.8116, -73.9465),
        ],
        # Deliberately graded outwards from the Williamsburg waterfront: four
        # venues inside a kilometre of each other, then rings at roughly 2, 5, 8
        # and 13 km. A feed that claims to be proximity-led should visibly change
        # as an explorer crosses those rings, and this is what makes that
        # testable rather than assertable.
        venues=[
            DemoVenue("Wythe Rooftop", "80 Wythe Ave, Brooklyn", 40.7220, -73.9578, "Williamsburg"),
            DemoVenue("Domino Park", "300 Kent Ave, Brooklyn", 40.7143, -73.9682, "Williamsburg"),
            DemoVenue("Berry Street Rooms", "160 Berry St", 40.7185, -73.9615, "Williamsburg"),
            DemoVenue("Grand Street Studio", "270 Grand St", 40.7118, -73.9440, "Williamsburg"),
            DemoVenue("McCarren Courts", "776 Lorimer St", 40.7205, -73.9501, "Greenpoint"),
            DemoVenue("Greenpoint Kilns", "67 West St", 40.7304, -73.9585, "Greenpoint"),
            DemoVenue("Tompkins Hall", "500 E 9th St", 40.7275, -73.9800, "East Village"),
            DemoVenue("Seaport Studio", "19 Fulton St", 40.7062, -74.0030, "Lower Manhattan"),
            DemoVenue(
                "Vanderbilt Rooms", "620 Vanderbilt Ave", 40.6786, -73.9686, "Prospect Heights"
            ),
            DemoVenue("Grand Army Lawn", "Prospect Park W", 40.6740, -73.9704, "Prospect Heights"),
            DemoVenue("Ditmars Social", "31-01 Ditmars Blvd", 40.7756, -73.9125, "Astoria"),
            DemoVenue("125th Street Stage", "253 W 125th St", 40.8090, -73.9500, "Harlem"),
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
            DemoListing(
                title="Berry Street Listening Room",
                venue="Berry Street Rooms",
                category="music",
                summary="One record played end to end, in the dark, no talking.",
                description=(
                    "Forty chairs facing a pair of horn speakers older than most of the "
                    "audience. Doors close when it starts and there is no re-entry, which "
                    "sounds precious until you have sat through one."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=15,
                duration_minutes=90,
                is_indoor=True,
                rating=4.6,
                rating_count=64,
                occurrences=(4, 27, 51),
            ),
            DemoListing(
                title="Grand Street Ferments",
                venue="Grand Street Studio",
                category="learning",
                summary="Two hours on brine ratios; you leave with three jars.",
                description=(
                    "Less mystical than it sounds - mostly weighing salt correctly and "
                    "learning what a failed batch smells like. Jars and lids included, and "
                    "you take home whatever you pack."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=40,
                duration_minutes=120,
                is_indoor=True,
                rating=4.5,
                rating_count=41,
                occurrences=(72, 240),
            ),
            DemoListing(
                title="Morning Courts at McCarren",
                venue="McCarren Courts",
                category="sports",
                summary="Open handball and basketball, first there gets the court.",
                description=(
                    "Busy from seven at weekends and almost empty on weekday mornings. No "
                    "booking and no fee; bring your own ball and expect to play whoever is "
                    "waiting."
                ),
                price_type="free",
                is_indoor=False,
                rating=4.2,
                rating_count=88,
            ),
            DemoListing(
                title="Greenpoint Kiln Open Day",
                venue="Greenpoint Kilns",
                category="arts-culture",
                summary="Twelve studios open their doors, once a month, free.",
                description=(
                    "Ceramics mostly, with a couple of glassblowers at the far end who draw "
                    "the crowd. Everything is for sale but nobody will follow you round the "
                    "room about it."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=300,
                is_indoor=True,
                rating=4.4,
                rating_count=112,
                occurrences=(50, 218),
            ),
            DemoListing(
                title="Vanderbilt Supper Club",
                venue="Vanderbilt Rooms",
                category="food-drink",
                summary="One sitting, one menu, eighteen strangers at a long table.",
                description=(
                    "The menu is decided the morning of, from whatever the market had. You "
                    "will be seated next to someone you did not arrive with, which is the "
                    "point of it."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=65,
                duration_minutes=180,
                is_indoor=True,
                rating=4.8,
                rating_count=203,
                occurrences=(29, 53, 197),
            ),
            DemoListing(
                title="Grand Army Lawn Kite Afternoon",
                venue="Grand Army Lawn",
                category="family",
                summary="Spare kites for anyone who turns up without one.",
                description=(
                    "Runs whenever there is wind and somebody with a car full of kites, "
                    "which in practice is most Saturdays. Small children flatten the whole "
                    "thing by four."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=180,
                is_indoor=False,
                rating=4.1,
                rating_count=37,
                occurrences=(78, 246),
            ),
            DemoListing(
                title="Ditmars Late Kitchen",
                venue="Ditmars Social",
                category="food-drink",
                summary="Greek grill that starts serving properly after eleven.",
                description=(
                    "Half the room is people finishing shifts elsewhere. The lamb is the "
                    "thing to order and the bread arrives whether you ask for it or not."
                ),
                price_type="range",
                price_amount=22,
                is_indoor=True,
                rating=4.5,
                rating_count=421,
            ),
            DemoListing(
                title="125th Street Revue",
                venue="125th Street Stage",
                category="music",
                summary="A house band, four guest singers, and a strict two-hour curfew.",
                description=(
                    "Been running long enough that the regulars know which songs get an "
                    "encore. Tickets at the door only, and the balcony is worth the extra "
                    "few dollars."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=35,
                duration_minutes=120,
                is_indoor=True,
                rating=4.7,
                rating_count=289,
                occurrences=(26, 194),
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
            ("King's Cross", 51.5350, -0.1250),
            ("Brixton", 51.4613, -0.1156),
            ("Greenwich", 51.4826, -0.0077),
            ("Walthamstow", 51.5860, -0.0200),
        ],
        venues=[
            DemoVenue("Rye Lane Arches", "133 Rye Ln, Peckham", 51.4712, -0.0699, "Peckham"),
            DemoVenue("Bussey Rooftop", "133 Rye Ln, Peckham", 51.4695, -0.0715, "Peckham"),
            DemoVenue("Peckham Rye Park", "Strakers Rd", 51.4600, -0.0640, "Peckham"),
            DemoVenue("Mare Street Works", "200 Mare St, Hackney", 51.5399, -0.0554, "Hackney"),
            DemoVenue("London Fields Lido", "London Fields West Side", 51.5410, -0.0620, "Hackney"),
            DemoVenue("Broadway Market Hall", "Broadway Mkt", 51.5370, -0.0615, "Hackney"),
            DemoVenue(
                "Riverside Terrace", "Belvedere Rd, South Bank", 51.5062, -0.1160, "South Bank"
            ),
            DemoVenue(
                "Coal Drops Arch", "Stable St, King's Cross", 51.5355, -0.1255, "King's Cross"
            ),
            DemoVenue("Brixton Basement", "Coldharbour Ln", 51.4620, -0.1140, "Brixton"),
            DemoVenue(
                "Greenwich Observatory Lawn", "Blackheath Ave", 51.4769, -0.0005, "Greenwich"
            ),
            DemoVenue("Blackhorse Workshop", "1-3 Sutherland Rd", 51.5865, -0.0215, "Walthamstow"),
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
            DemoListing(
                title="Bussey Rooftop Film Nights",
                venue="Bussey Rooftop",
                category="arts-culture",
                summary="Deckchairs, a bedsheet screen, and headphones so the neighbours sleep.",
                description=(
                    "Mostly repertory, occasionally something nobody has heard of. Blankets "
                    "are free at the door and you will want one by the second reel."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=12,
                duration_minutes=150,
                is_indoor=False,
                rating=4.4,
                rating_count=176,
                occurrences=(5, 29, 197),
            ),
            DemoListing(
                title="Peckham Rye Morning Run",
                venue="Peckham Rye Park",
                category="sports",
                summary="Five kilometres, timed, free, and nobody minds if you walk it.",
                description=(
                    "Two laps of the park on grass and gravel. Turn up ten minutes early to "
                    "be pointed at the start; the finish funnel is the only organised part."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=60,
                is_indoor=False,
                rating=4.6,
                rating_count=298,
                occurrences=(80, 248),
            ),
            DemoListing(
                title="London Fields Lido Early Swim",
                venue="London Fields Lido",
                category="wellness",
                summary="Fifty metres, heated, open before it is light.",
                description=(
                    "Lane swimming from half six, and it is genuinely quiet for the first "
                    "hour. Book the night before in winter or you will not get in."
                ),
                price_type="fixed",
                price_amount=7,
                duration_minutes=60,
                is_indoor=False,
                rating=4.5,
                rating_count=512,
            ),
            DemoListing(
                title="Broadway Market Saturday",
                venue="Broadway Market Hall",
                category="markets",
                summary="A hundred stalls along one street, from nine until four.",
                description=(
                    "Food at the canal end, secondhand books and records in the middle. Come "
                    "before eleven if you want to move at your own pace."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=420,
                is_indoor=False,
                rating=4.3,
                rating_count=387,
                occurrences=(74, 242),
            ),
            DemoListing(
                title="Coal Drops Late Openings",
                venue="Coal Drops Arch",
                category="arts-culture",
                summary="Galleries in the arches stay open until nine, first Thursday.",
                description=(
                    "Small spaces, six of them, walkable in an hour. The one at the far end "
                    "does the interesting things and is always the emptiest."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=180,
                is_indoor=True,
                rating=4.2,
                rating_count=94,
                occurrences=(31, 199),
            ),
            DemoListing(
                title="Brixton Basement Soundsystem",
                venue="Brixton Basement",
                category="nightlife",
                summary="Reggae and dub on a rig that predates everyone dancing to it.",
                description=(
                    "Low ceiling, serious bass, and a crowd that spans about forty years. "
                    "Cash at the door and it fills by midnight. The sound is set up for "
                    "weight rather than volume, so you can still hear someone next to you."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=15,
                duration_minutes=300,
                is_indoor=True,
                rating=4.7,
                rating_count=341,
                occurrences=(46, 214),
            ),
            DemoListing(
                title="Observatory Lawn Stargazing",
                venue="Greenwich Observatory Lawn",
                category="learning",
                summary="Telescopes on the hill, volunteers pointing them at things.",
                description=(
                    "Cancelled without ceremony if it clouds over, which it often does. When "
                    "it is clear you will queue, and it will be worth it."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=120,
                is_indoor=False,
                rating=4.5,
                rating_count=128,
                occurrences=(8, 32, 200),
            ),
            DemoListing(
                title="Blackhorse Open Workshop",
                venue="Blackhorse Workshop",
                category="learning",
                summary="A public workshop with real machines and supervision.",
                description=(
                    "Day rate for the bench, induction required for anything that spins. "
                    "Wood at the front, metal at the back, and a good scrap bin."
                ),
                price_type="fixed",
                price_amount=20,
                duration_minutes=480,
                is_indoor=True,
                rating=4.6,
                rating_count=73,
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
            ("Parklands", -1.2620, 36.8180),
            ("Kilimani", -1.2900, 36.7850),
            ("Karura", -1.2400, 36.8300),
            ("Lavington", -1.2790, 36.7700),
        ],
        venues=[
            DemoVenue(
                "Westlands Yard", "Woodvale Grove, Westlands", -1.2665, 36.8036, "Westlands"
            ),
            DemoVenue("Sarit Rooftop", "Karuna Rd, Westlands", -1.2610, 36.8025, "Westlands"),
            DemoVenue("Karen Forest Edge", "Karen Rd", -1.3204, 36.7118, "Karen"),
            DemoVenue(
                "Kenyatta Avenue Hall",
                "Kenyatta Ave",
                -1.2845,
                36.8220,
                "Central Business District",
            ),
            DemoVenue(
                "Gikomba Lanes", "Gikomba Mkt", -1.2810, 36.8420, "Central Business District"
            ),
            DemoVenue("Parklands Court", "3rd Ave Parklands", -1.2624, 36.8188, "Parklands"),
            DemoVenue("Kilimani Rooms", "Wood Ave, Kilimani", -1.2905, 36.7860, "Kilimani"),
            DemoVenue("Karura Waterfall Gate", "Limuru Rd", -1.2405, 36.8305, "Karura"),
            DemoVenue("Lavington Green Studio", "James Gichuru Rd", -1.2795, 36.7705, "Lavington"),
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
            DemoListing(
                title="Sarit Rooftop Sundowner",
                venue="Sarit Rooftop",
                category="nightlife",
                summary="Amapiano and afrobeats from six, above the traffic.",
                description=(
                    "Gets going earlier than most places because everyone is avoiding the "
                    "drive home. Kitchen closes at ten, music does not. The far corner by "
                    "the planters is the only quiet place to hold a conversation."
                ),
                kind=TYPE_EVENT,
                price_type="range",
                price_amount=800,
                duration_minutes=300,
                is_indoor=False,
                rating=4.4,
                rating_count=211,
                occurrences=(7, 31, 199),
            ),
            DemoListing(
                title="Gikomba Fabric Run",
                venue="Gikomba Lanes",
                category="markets",
                summary="Bolt cloth and secondhand by the kilo, if you can handle the crush.",
                description=(
                    "Enormous, loud, and organised in a way that only makes sense after an "
                    "hour. Go early with small notes and a bag you can hold in front of you."
                ),
                price_type="free",
                is_indoor=False,
                rating=4.2,
                rating_count=163,
            ),
            DemoListing(
                title="Parklands Evening Cricket",
                venue="Parklands Court",
                category="sports",
                summary="Tape-ball under lights, teams made up on the spot.",
                description=(
                    "Turn up and you will be batting within twenty minutes. More social than "
                    "competitive, though nobody will tell you that while bowling."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=180,
                is_indoor=False,
                rating=4.3,
                rating_count=57,
                occurrences=(9, 33, 201),
            ),
            DemoListing(
                title="Kilimani Poetry Rooms",
                venue="Kilimani Rooms",
                category="arts-culture",
                summary="Open mic first, invited readers after the break.",
                description=(
                    "Sign up on the sheet by the door before seven. The second half is "
                    "consistently the better one and most people stay for it."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=300,
                duration_minutes=150,
                is_indoor=True,
                rating=4.6,
                rating_count=98,
                occurrences=(27, 195),
            ),
            DemoListing(
                title="Karura Waterfall Trail",
                venue="Karura Waterfall Gate",
                category="outdoors",
                summary="Twelve kilometres of forest track inside the city, gated and safe.",
                description=(
                    "Bikes allowed on the wide paths, and the waterfall is a twenty-minute "
                    "walk from the Limuru Road gate. Entry is paid at the gate in cash."
                ),
                price_type="fixed",
                price_amount=600,
                duration_minutes=180,
                is_indoor=False,
                rating=4.8,
                rating_count=442,
            ),
            DemoListing(
                title="Lavington Green Pottery",
                venue="Lavington Green Studio",
                category="learning",
                summary="Wheel time by the hour with a tutor who leaves you alone.",
                description=(
                    "Four wheels, booked in two-hour blocks. Firing is extra and takes about "
                    "a fortnight, so it is not a same-day souvenir. Beginners are put on "
                    "the wheel nearest the door, which is the one with the steadier pedal."
                ),
                price_type="fixed",
                price_amount=1500,
                duration_minutes=120,
                is_indoor=True,
                rating=4.5,
                rating_count=66,
            ),
        ],
    ),
    DemoCity(
        name="Tokyo",
        slug="tokyo",
        country="Japan",
        country_code="JP",
        timezone="Asia/Tokyo",
        # Zero-decimal: 500 yen is 500 minor units, not 50000. Included partly to
        # keep a zero-decimal currency in the demo set, because the money path is
        # the one place a wrong assumption is silently off by a hundred.
        currency="JPY",
        languages=["ja", "en"],
        latitude=35.6762,
        longitude=139.6503,
        neighbourhoods=[
            ("Shimokitazawa", 35.6613, 139.6680),
            ("Kiyosumi", 35.6820, 139.7990),
            ("Nakameguro", 35.6440, 139.6990),
            ("Yanaka", 35.7270, 139.7660),
        ],
        venues=[
            DemoVenue("Shimokita Basement", "2-14 Kitazawa", 35.6615, 139.6675, "Shimokitazawa"),
            DemoVenue("Kitazawa Rooftop", "2-26 Kitazawa", 35.6630, 139.6690, "Shimokitazawa"),
            DemoVenue("Kiyosumi Warehouse", "1-3 Kiyosumi", 35.6815, 139.7985, "Kiyosumi"),
            DemoVenue("Kiyosumi Garden Gate", "3-3 Kiyosumi", 35.6800, 139.7955, "Kiyosumi"),
            DemoVenue("Meguro Riverside", "1-10 Aobadai", 35.6455, 139.6985, "Nakameguro"),
            DemoVenue("Yanaka Ginza Steps", "3-13 Yanaka", 35.7275, 139.7655, "Yanaka"),
        ],
        listings=[
            DemoListing(
                title="Shimokita Record Bar Session",
                venue="Shimokita Basement",
                category="music",
                summary="Ten seats, one turntable, and whatever the owner feels like.",
                description=(
                    "Requests are taken and quietly ignored. Cover charge includes the first "
                    "drink and there is a strict no-photographs rule that is actually enforced."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=2000,
                duration_minutes=180,
                is_indoor=True,
                rating=4.7,
                rating_count=147,
                occurrences=(5, 29, 53),
            ),
            DemoListing(
                title="Kitazawa Rooftop Natural Wine",
                venue="Kitazawa Rooftop",
                category="food-drink",
                summary="Six wines by the glass, standing room, closes at eleven sharp.",
                description=(
                    "Small enough that the pours are generous and the list changes weekly. "
                    "Standing only, and the queue outside moves quickly."
                ),
                price_type="range",
                price_amount=1200,
                is_indoor=False,
                rating=4.4,
                rating_count=203,
            ),
            DemoListing(
                title="Kiyosumi Warehouse Exhibition",
                venue="Kiyosumi Warehouse",
                category="arts-culture",
                summary="Three floors of a converted store, contemporary, changes quarterly.",
                description=(
                    "The top floor is the one worth the stairs. Free on the first Monday, "
                    "otherwise a modest ticket bought from a machine in the lobby."
                ),
                price_type="fixed",
                price_amount=800,
                duration_minutes=90,
                is_indoor=True,
                rating=4.5,
                rating_count=176,
            ),
            DemoListing(
                title="Kiyosumi Garden Morning",
                venue="Kiyosumi Garden Gate",
                category="outdoors",
                summary="A stroll garden with stepping stones across the pond, quiet at opening.",
                description=(
                    "Get there for opening and you will have the stones to yourself for "
                    "twenty minutes. Small entry fee, paid in coins at the gate."
                ),
                price_type="fixed",
                price_amount=150,
                duration_minutes=60,
                is_indoor=False,
                rating=4.6,
                rating_count=389,
            ),
            DemoListing(
                title="Meguro Riverside Hanami Walk",
                venue="Meguro Riverside",
                category="outdoors",
                summary="Four kilometres of cherry trees along a narrow canal, free.",
                description=(
                    "Unbearable at peak bloom and lovely a week either side. The stalls "
                    "appear overnight and vanish just as fast. Walk it from the south end "
                    "and you will be facing the better light the whole way."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=120,
                is_indoor=False,
                rating=4.3,
                rating_count=512,
                occurrences=(30, 198),
            ),
            DemoListing(
                title="Yanaka Ginza Evening Stalls",
                venue="Yanaka Ginza Steps",
                category="markets",
                summary="An old shopping street that still sells croquettes at the steps.",
                description=(
                    "Best at dusk when the sun sits at the end of the street. Cash only in "
                    "most of it, and several places shut on Mondays. The steps at the "
                    "western end are where everyone stops to eat what they have bought."
                ),
                price_type="free",
                is_indoor=False,
                rating=4.5,
                rating_count=298,
            ),
            DemoListing(
                title="Yanaka Morning Zazen",
                venue="Yanaka Ginza Steps",
                category="wellness",
                summary="Forty minutes of sitting, in a temple hall, before work.",
                description=(
                    "Beginners welcome and briefly instructed. No booking; arrive ten minutes "
                    "early or you will not be let in. Socks rather than bare feet, and the "
                    "hall is genuinely cold before about May."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=40,
                is_indoor=True,
                rating=4.8,
                rating_count=84,
                occurrences=(15, 39, 63),
            ),
        ],
    ),
    DemoCity(
        name="Paris",
        slug="paris",
        country="France",
        country_code="FR",
        timezone="Europe/Paris",
        currency="EUR",
        languages=["fr", "en"],
        latitude=48.8566,
        longitude=2.3522,
        neighbourhoods=[
            ("Belleville", 48.8720, 2.3820),
            ("Canal Saint-Martin", 48.8710, 2.3660),
            ("Le Marais", 48.8590, 2.3600),
            ("Butte-aux-Cailles", 48.8280, 2.3500),
            ("Oberkampf", 48.8645, 2.3705),
            ("Jardin des Plantes", 48.8425, 2.3560),
            ("Bois de Boulogne", 48.8780, 2.2640),
        ],
        venues=[
            DemoVenue(
                "Belleville Terrasse",
                "Rue Denoyez",
                48.8722,
                2.3812,
                "Belleville",
                facilities=("outdoor_seating", "heated", "covered", "card_accepted"),
            ),
            DemoVenue(
                "Parc de Belleville",
                "47 Rue des Couronnes",
                48.8705,
                2.3835,
                "Belleville",
                facilities=("childrens_play_area", "step_free_access", "pushchair_access"),
            ),
            DemoVenue("Quai de Valmy", "Quai de Valmy", 48.8715, 2.3665, "Canal Saint-Martin"),
            DemoVenue(
                "Marais Atelier",
                "12 Rue de Turenne",
                48.8585,
                2.3625,
                "Le Marais",
                facilities=("indoor_seating", "heated", "step_free_access", "accessible_toilet"),
            ),
            DemoVenue(
                "Cailles Cave", "Rue des Cinq-Diamants", 48.8275, 2.3495, "Butte-aux-Cailles"
            ),
            DemoVenue(
                "Jardin d'Acclimatation Gate",
                "Bois de Boulogne",
                48.8778,
                2.2635,
                "Bois de Boulogne",
                facilities=(
                    "childrens_play_area",
                    "child_menu",
                    "high_chairs",
                    "baby_changing",
                    "step_free_access",
                    "pushchair_access",
                    "parking",
                ),
            ),
            DemoVenue(
                "Le Potager Vert",
                "24 Rue Oberkampf",
                48.8645,
                2.3705,
                "Oberkampf",
                facilities=("indoor_seating", "heated", "step_free_access", "high_chairs"),
            ),
            DemoVenue(
                "Grande Mosquée Tea Room",
                "39 Rue Geoffroy-Saint-Hilaire",
                48.8420,
                2.3552,
                "Jardin des Plantes",
                facilities=("indoor_seating", "covered", "prayer_room", "shaded_seating"),
            ),
            DemoVenue(
                "Muséum Grande Galerie",
                "36 Rue Geoffroy-Saint-Hilaire",
                48.8430,
                2.3565,
                "Jardin des Plantes",
                facilities=(
                    "indoor_seating",
                    "heated",
                    "step_free_access",
                    "accessible_toilet",
                    "pushchair_access",
                    "baby_changing",
                ),
            ),
        ],
        listings=[
            DemoListing(
                title="Belleville Terrasse Apéro",
                venue="Belleville Terrasse",
                category="food-drink",
                summary="Natural wine and small plates on a street of murals.",
                description=(
                    "Tables spill into the road once the light goes. No reservations, so put "
                    "your name down and walk up the hill while you wait."
                ),
                price_type="range",
                price_amount=14,
                is_indoor=False,
                rating=4.4,
                rating_count=267,
                suitability=("vegetarian", "outdoor_seating", "heated", "serves_late"),
            ),
            DemoListing(
                title="Parc de Belleville Sunset",
                venue="Parc de Belleville",
                category="outdoors",
                summary="The best free view of the city, from the top of the steps.",
                description=(
                    "Steeper than it looks and worth it. People bring bottles and stay until "
                    "the park closes, which is enforced politely but firmly."
                ),
                price_type="free",
                is_indoor=False,
                rating=4.7,
                rating_count=634,
            ),
            DemoListing(
                title="Canal Saint-Martin Evening Boules",
                venue="Quai de Valmy",
                category="sports",
                summary="Pétanque by the water, sets borrowed from whoever has one.",
                description=(
                    "Informal to the point of chaos and all the better for it. The gravel "
                    "strip north of the footbridge is where the good players end up."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=180,
                is_indoor=False,
                rating=4.2,
                rating_count=112,
                occurrences=(6, 30, 198),
            ),
            DemoListing(
                title="Marais Atelier Life Drawing",
                venue="Marais Atelier",
                category="arts-culture",
                summary="Three hours, one model, no instruction and no talking.",
                description=(
                    "Paper and boards provided, bring your own charcoal. Poses run from two "
                    "minutes to forty-five and the long one is the point."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=18,
                duration_minutes=180,
                is_indoor=True,
                rating=4.6,
                rating_count=143,
                occurrences=(28, 196),
            ),
            DemoListing(
                title="Butte-aux-Cailles Cellar Sessions",
                venue="Cailles Cave",
                category="music",
                summary="Chanson and jazz manouche in a stone cellar, two sets.",
                description=(
                    "Low vaulted ceiling and about fifty chairs. The second set runs late and "
                    "the last métro is the real curfew. Tables at the back are further from "
                    "the music and much easier to talk at."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=16,
                duration_minutes=150,
                is_indoor=True,
                rating=4.5,
                rating_count=189,
                occurrences=(4, 52, 220),
            ),
            # The five listings below exist so a constrained request has both
            # answers and non-answers in the same city. A demo catalogue where
            # everything satisfies every constraint cannot show a filter working,
            # and one where nothing does cannot either.
            DemoListing(
                title="Jardin d'Acclimatation Afternoon",
                venue="Jardin d'Acclimatation Gate",
                category="outdoors",
                summary="A children's garden with rides, goats and a lot of running.",
                description=(
                    "Old-fashioned in the best way. The little train loops the whole park, "
                    "and the playground at the north end is where everyone under ten ends "
                    "up. Buy the ride tickets in a book rather than singly."
                ),
                price_type="range",
                price_amount=7,
                duration_minutes=210,
                is_indoor=False,
                rating=4.3,
                rating_count=1204,
                suitability=(
                    "childrens_play_area",
                    "child_menu",
                    "child_friendly",
                    "pushchair_access",
                    "high_chairs",
                ),
            ),
            DemoListing(
                title="Le Potager Vert Table d'Hôte",
                venue="Le Potager Vert",
                category="food-drink",
                summary="One vegan menu a night, written on the wall at six.",
                description=(
                    "Everything is plant-based and nobody makes a thing of it. Four courses, "
                    "no choice, and the kitchen will work around nuts and gluten if you say "
                    "when you book rather than when you sit down."
                ),
                price_type="fixed",
                price_amount=32,
                duration_minutes=120,
                is_indoor=True,
                rating=4.7,
                rating_count=311,
                suitability=(
                    "vegan",
                    "vegetarian",
                    "dairy_free",
                    "nut_free",
                    "gluten_free",
                    "child_friendly",
                ),
            ),
            DemoListing(
                title="Mint Tea at the Grande Mosquée",
                venue="Grande Mosquée Tea Room",
                category="food-drink",
                summary="Sweet mint tea and pastries under the fig trees.",
                description=(
                    "The courtyard is the reason to come and the tiled salon is where you go "
                    "when it rains. Table service is slow by design. Cash is easier than "
                    "card at the pastry counter."
                ),
                price_type="range",
                price_amount=6,
                duration_minutes=75,
                is_indoor=True,
                rating=4.4,
                rating_count=892,
                suitability=(
                    "halal",
                    "vegetarian",
                    "alcohol_free",
                    "serves_late",
                    "child_friendly",
                ),
            ),
            DemoListing(
                title="Grande Galerie de l'Évolution",
                venue="Muséum Grande Galerie",
                category="arts-culture",
                summary="The great procession of animals, four floors under one glass roof.",
                description=(
                    "Worth an hour even if museums are not usually the thing. The lighting "
                    "shifts through a day cycle on the hour. Lifts reach every floor and the "
                    "cloakroom will take a pushchair."
                ),
                price_type="fixed",
                price_amount=13,
                duration_minutes=120,
                is_indoor=True,
                rating=4.6,
                rating_count=2140,
                suitability=(
                    "step_free_access",
                    "accessible_toilet",
                    "child_friendly",
                    "pushchair_access",
                    "baby_changing",
                ),
            ),
            DemoListing(
                title="Belleville Vegan Market Stall",
                venue="Belleville Terrasse",
                category="markets",
                summary="A dozen producers, all plant-based, Saturday mornings only.",
                description=(
                    "Small and busy. The bread goes first and the cheese substitutes are "
                    "better than they have any right to be. Covered when it rains, heated "
                    "when it does not stop."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=150,
                is_indoor=False,
                rating=4.2,
                rating_count=87,
                suitability=("vegan", "vegetarian", "dairy_free", "covered", "heated"),
                occurrences=(34, 202, 370),
            ),
        ],
    ),
    DemoCity(
        name="Mexico City",
        slug="mexico-city",
        country="Mexico",
        country_code="MX",
        timezone="America/Mexico_City",
        currency="MXN",
        languages=["es", "en"],
        latitude=19.4326,
        longitude=-99.1332,
        neighbourhoods=[
            ("Roma Norte", 19.4180, -99.1600),
            ("Coyoacán", 19.3500, -99.1620),
            ("Centro Histórico", 19.4340, -99.1330),
        ],
        venues=[
            DemoVenue("Roma Azotea", "Calle Orizaba", 19.4185, -99.1605, "Roma Norte"),
            DemoVenue("Mercado Medellín", "Calle Campeche", 19.4110, -99.1640, "Roma Norte"),
            DemoVenue("Coyoacán Plaza Stage", "Jardín Centenario", 19.3505, -99.1625, "Coyoacán"),
            DemoVenue("Centro Rooftop", "Calle Madero", 19.4335, -99.1355, "Centro Histórico"),
        ],
        listings=[
            DemoListing(
                title="Roma Azotea Mezcal Tasting",
                venue="Roma Azotea",
                category="food-drink",
                summary="Six mezcals, explained properly, on a roof above the city.",
                description=(
                    "Small groups and a host who will talk about agave for as long as you "
                    "let them. Includes food, which you will want. Book the earlier sitting "
                    "if you care about the view rather than the drinking."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=650,
                duration_minutes=120,
                is_indoor=False,
                rating=4.7,
                rating_count=221,
                occurrences=(7, 31, 199),
            ),
            DemoListing(
                title="Mercado Medellín Morning",
                venue="Mercado Medellín",
                category="markets",
                summary="South American groceries and a very good ceviche counter.",
                description=(
                    "Colombian and Venezuelan stalls at the north end. Eat at the counters "
                    "rather than taking it away; it is half the experience."
                ),
                price_type="free",
                is_indoor=True,
                rating=4.5,
                rating_count=412,
            ),
            DemoListing(
                title="Coyoacán Weekend Danzón",
                venue="Coyoacán Plaza Stage",
                category="music",
                summary="A live band and a hundred couples who have done this for decades.",
                description=(
                    "Free, outdoors, and entirely unbothered by onlookers. Someone will "
                    "eventually pull you in, so decide in advance how you feel about that."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=180,
                is_indoor=False,
                rating=4.8,
                rating_count=356,
                occurrences=(76, 244),
            ),
            DemoListing(
                title="Centro Rooftop Cathedral View",
                venue="Centro Rooftop",
                category="food-drink",
                summary="Coffee, a terrace, and the cathedral close enough to see the stonework.",
                description=(
                    "Go up for the view and stay for the breakfast. Busiest at ten, empty by "
                    "half four when the light is actually best. There is a lift but the "
                    "queue for it is longer than the four flights of stairs."
                ),
                price_type="range",
                price_amount=180,
                is_indoor=False,
                rating=4.3,
                rating_count=298,
            ),
        ],
    ),
    DemoCity(
        name="Berlin",
        slug="berlin",
        country="Germany",
        country_code="DE",
        timezone="Europe/Berlin",
        currency="EUR",
        languages=["de", "en"],
        latitude=52.5200,
        longitude=13.4050,
        neighbourhoods=[
            ("Neukölln", 52.4810, 13.4350),
            ("Kreuzberg", 52.4990, 13.4180),
            ("Prenzlauer Berg", 52.5390, 13.4240),
        ],
        venues=[
            DemoVenue("Weserstraße Bar", "Weserstr. 40", 52.4865, 13.4285, "Neukölln"),
            DemoVenue("Tempelhofer Feld Gate", "Oderstr.", 52.4790, 13.4200, "Neukölln"),
            DemoVenue("Kreuzberg Hof", "Oranienstr. 190", 52.5010, 13.4210, "Kreuzberg"),
            DemoVenue("Mauerpark Steps", "Bernauer Str. 63", 52.5405, 13.4025, "Prenzlauer Berg"),
        ],
        listings=[
            DemoListing(
                title="Weserstraße Late Bar",
                venue="Weserstraße Bar",
                category="nightlife",
                summary="Opens at nine, fills at two, no sign on the door.",
                description=(
                    "One long room, a good sound system nobody shows off about, and cash "
                    "only. The smoking room is where the conversations happen."
                ),
                price_type="range",
                price_amount=5,
                is_indoor=True,
                rating=4.3,
                rating_count=276,
            ),
            DemoListing(
                title="Tempelhofer Feld Sunset Skate",
                venue="Tempelhofer Feld Gate",
                category="sports",
                summary="A disused runway, two kilometres long, free to everyone.",
                description=(
                    "Skaters, kite-buggies and people learning to cycle, all somehow "
                    "coexisting. The wind is the deciding factor; there is nothing to block it."
                ),
                price_type="free",
                is_indoor=False,
                rating=4.8,
                rating_count=701,
            ),
            DemoListing(
                title="Kreuzberg Hof Kino",
                venue="Kreuzberg Hof",
                category="arts-culture",
                summary="Films in a courtyard, subtitled, blankets provided.",
                description=(
                    "Starts when it is properly dark, so later in summer than you expect. "
                    "Rain cancels it and they announce that late. Bring something to sit "
                    "on; the benches run out well before the courtyard does."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=9,
                duration_minutes=150,
                is_indoor=False,
                rating=4.5,
                rating_count=184,
                occurrences=(5, 29, 197),
            ),
            DemoListing(
                title="Mauerpark Sunday Karaoke",
                venue="Mauerpark Steps",
                category="family",
                summary="An amphitheatre of strangers, a man with a bike-powered PA, and no shame.",
                description=(
                    "Thousands of people on the steps by mid-afternoon. Free, chaotic, and "
                    "genuinely one of the better things the city does. Put your name down "
                    "early if you intend to sing, because the list closes long before the end."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=240,
                is_indoor=False,
                rating=4.6,
                rating_count=823,
                occurrences=(78, 246),
            ),
        ],
    ),
    DemoCity(
        name="São Paulo",
        slug="sao-paulo",
        country="Brazil",
        country_code="BR",
        timezone="America/Sao_Paulo",
        currency="BRL",
        languages=["pt", "en"],
        latitude=-23.5505,
        longitude=-46.6333,
        neighbourhoods=[
            ("Vila Madalena", -23.5540, -46.6900),
            ("Liberdade", -23.5590, -46.6350),
            ("Ibirapuera", -23.5870, -46.6570),
        ],
        venues=[
            DemoVenue("Beco do Batman", "R. Gonçalo Afonso", -23.5545, -46.6905, "Vila Madalena"),
            DemoVenue("Madalena Boteco", "R. Aspicuelta", -23.5555, -46.6885, "Vila Madalena"),
            DemoVenue("Liberdade Arcade", "R. Galvão Bueno", -23.5585, -46.6355, "Liberdade"),
            DemoVenue(
                "Ibirapuera Gate 3", "Av. Pedro Álvares Cabral", -23.5875, -46.6575, "Ibirapuera"
            ),
        ],
        listings=[
            DemoListing(
                title="Beco do Batman Walking Tour",
                venue="Beco do Batman",
                category="arts-culture",
                summary="An alley repainted constantly, walked with someone who knows it.",
                description=(
                    "Ninety minutes and it will change again before you get back. Tips only, "
                    "which means the guides are good. Go on a weekday if you want to "
                    "photograph the walls without twenty people in front of them."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=90,
                is_indoor=False,
                rating=4.6,
                rating_count=334,
                occurrences=(26, 194),
            ),
            DemoListing(
                title="Madalena Samba de Roda",
                venue="Madalena Boteco",
                category="music",
                summary="A circle of musicians at a bar table, no stage, from eight.",
                description=(
                    "Standing room in the street outside once it fills, which it does. Order "
                    "at the window and do not expect to hear yourself think."
                ),
                kind=TYPE_EVENT,
                price_type="range",
                price_amount=30,
                duration_minutes=240,
                is_indoor=False,
                rating=4.7,
                rating_count=428,
                occurrences=(6, 30, 198),
            ),
            DemoListing(
                title="Liberdade Sunday Market",
                venue="Liberdade Arcade",
                category="markets",
                summary="Japanese-Brazilian street food under red lanterns, weekends only.",
                description=(
                    "Yakisoba and pastel from the same block, which tells you most of what "
                    "you need to know about the neighbourhood. Cash is easier."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=360,
                is_indoor=False,
                rating=4.4,
                rating_count=512,
                occurrences=(74, 242),
            ),
            DemoListing(
                title="Ibirapuera Morning Cycle",
                venue="Ibirapuera Gate 3",
                category="outdoors",
                summary="Closed roads, borrowed bikes, and a park that actually works.",
                description=(
                    "Roads inside the park close to cars on Sundays. Bike hire at gate three "
                    "and a queue for it by nine. The outer loop is flat and the inner one "
                    "is where everyone races, so pick according to mood."
                ),
                price_type="free",
                is_indoor=False,
                rating=4.7,
                rating_count=655,
            ),
        ],
    ),
    DemoCity(
        name="Mumbai",
        slug="mumbai",
        country="India",
        country_code="IN",
        timezone="Asia/Kolkata",
        currency="INR",
        languages=["hi", "mr", "en"],
        latitude=19.0760,
        longitude=72.8777,
        neighbourhoods=[
            ("Bandra West", 19.0600, 72.8300),
            ("Colaba", 18.9220, 72.8330),
            ("Dadar", 19.0180, 72.8440),
        ],
        venues=[
            DemoVenue("Bandra Bandstand", "Bandstand Promenade", 19.0455, 72.8195, "Bandra West"),
            DemoVenue("Pali Hill Rooms", "Pali Hill", 19.0645, 72.8290, "Bandra West"),
            DemoVenue("Colaba Causeway Steps", "Colaba Causeway", 18.9215, 72.8320, "Colaba"),
            DemoVenue("Dadar Flower Lane", "Senapati Bapat Marg", 19.0185, 72.8435, "Dadar"),
        ],
        listings=[
            DemoListing(
                title="Bandstand Evening Walk",
                venue="Bandra Bandstand",
                category="outdoors",
                summary="A sea wall, the sunset, and half the neighbourhood out walking.",
                description=(
                    "Free and busy from five. The rocks are lower at the south end if you "
                    "want to sit closer to the water. Sunset is the point of it, and the "
                    "crowd thins noticeably within half an hour of it."
                ),
                price_type="free",
                is_indoor=False,
                rating=4.5,
                rating_count=487,
            ),
            DemoListing(
                title="Pali Hill Listening Session",
                venue="Pali Hill Rooms",
                category="music",
                summary="Hindustani classical, two hours, thirty cushions on the floor.",
                description=(
                    "Starts on time, which is unusual, and latecomers wait for the first "
                    "break. Chai afterwards is included and most people stay for it."
                ),
                kind=TYPE_EVENT,
                price_type="fixed",
                price_amount=800,
                duration_minutes=120,
                is_indoor=True,
                rating=4.8,
                rating_count=132,
                occurrences=(28, 196),
            ),
            DemoListing(
                title="Colaba Causeway Browse",
                venue="Colaba Causeway Steps",
                category="markets",
                summary="Street stalls the length of the road, from silver to secondhand books.",
                description=(
                    "Haggling expected and largely good-natured. The bookstalls near the "
                    "north end are the ones worth the time. Prices start high for anyone "
                    "who looks like they have just arrived, which is most people."
                ),
                price_type="free",
                is_indoor=False,
                rating=4.2,
                rating_count=623,
            ),
            DemoListing(
                title="Dadar Flower Market Dawn",
                venue="Dadar Flower Lane",
                category="markets",
                summary="Marigolds by the sack from four in the morning.",
                description=(
                    "Over by nine and best an hour before sunrise. Wear shoes you do not "
                    "mind soaking; the lane is washed constantly. The wholesale end is "
                    "north of the bridge and is where the volume actually is."
                ),
                kind=TYPE_EVENT,
                price_type="free",
                duration_minutes=120,
                is_indoor=False,
                rating=4.6,
                rating_count=241,
                occurrences=(20, 44, 212),
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
            found.facilities = suitability_vocab.normalise(list(item.facilities))
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
            found.suitability = suitability_vocab.normalise(list(listing.suitability))
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

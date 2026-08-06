"""Addis Ababa pilot seed data.

Addis Ababa is the pilot city named throughout the specs (spec 55.05 uses
``?city=addis-ababa``; spec 82.01 pairs Stripe with Chapa for local payments).

The data is real in outline - genuine neighbourhoods, venues and institutions with
approximately correct coordinates - so that geospatial ranking, "near me" search
and distance explanations behave the way they will in production. Event times are
generated relative to the run date so the "tonight" and "this weekend" rails always
have content to show.

Prices are in ETB. Idempotent: re-running updates rather than duplicating.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.domains.catalog.models import (
    STATUS_PUBLISHED,
    TYPE_ACTIVITY,
    TYPE_EVENT,
    TYPE_PLACE,
    Category,
    City,
    EventInstance,
    Experience,
    Media,
    Neighborhood,
    Tag,
    Venue,
)
from app.domains.publisher.models import (
    TRUST_LEVEL_STRATEGIC_PARTNER,
    TRUST_LEVEL_TRUSTED_ORG,
    TRUST_LEVEL_VERIFIED,
    Publisher,
)

# Registers every domain on Base.metadata. Required here because publishers holds a
# foreign key into identity.users, which SQLAlchemy cannot resolve unless that
# domain's models have been imported too.
from app.models import Base  # noqa: F401

logger = get_logger("mado.seed")

# Deterministic so repeated seeds produce the same catalogue, which makes the
# ranking behaviour reproducible when debugging.
RNG = random.Random(20260805)

CITY_TZ = ZoneInfo("Africa/Addis_Ababa")

CITY = {
    "name": "Addis Ababa",
    "slug": "addis-ababa",
    "country": "Ethiopia",
    "country_code": "ET",
    "timezone": "Africa/Addis_Ababa",
    "currency": "ETB",
    "languages": ["am", "en", "om"],
    "latitude": 9.0192,
    "longitude": 38.7525,
    "is_live": True,
}

NEIGHBORHOODS = [
    (
        "Bole",
        "bole",
        "Airport-side district of hotels, restaurants and nightlife.",
        8.9945,
        38.7896,
    ),
    (
        "Piassa",
        "piassa",
        "The old commercial heart, full of Armenian and Italian-era architecture.",
        9.0348,
        38.7503,
    ),
    (
        "Kazanchis",
        "kazanchis",
        "Business and conference district around the UN compounds.",
        9.0125,
        38.7686,
    ),
    ("Arat Kilo", "arat-kilo", "University quarter, museums and student cafes.", 9.0349, 38.7626),
    (
        "Sarbet",
        "sarbet",
        "Residential south-west with a growing cafe and gallery scene.",
        8.9958,
        38.7412,
    ),
    ("Merkato", "merkato", "One of Africa's largest open-air markets.", 9.0333, 38.7333),
    (
        "Entoto",
        "entoto",
        "Forested ridge above the city with viewpoints and trails.",
        9.0866,
        38.7614,
    ),
    (
        "Old Airport",
        "old-airport",
        "Leafy diplomatic neighbourhood with quiet restaurants.",
        8.9906,
        38.7245,
    ),
]

CATEGORIES = [
    ("Food & Drink", "food-drink", "utensils", 1),
    ("Music", "music", "music", 2),
    ("Arts & Culture", "arts-culture", "palette", 3),
    ("Outdoors", "outdoors", "mountain", 4),
    ("Nightlife", "nightlife", "moon", 5),
    ("Learning", "learning", "graduation-cap", 6),
    ("Markets", "markets", "shopping-bag", 7),
    ("Sports", "sports", "trophy", 8),
    ("Family", "family", "users", 9),
    ("Wellness", "wellness", "heart", 10),
]

TAGS = [
    ("Free", "free"),
    ("Hidden gem", "hidden-gem"),
    ("Family friendly", "family-friendly"),
    ("Outdoor", "outdoor"),
    ("Indoor", "indoor"),
    ("Live music", "live-music"),
    ("Coffee", "coffee"),
    ("Traditional", "traditional"),
    ("Late night", "late-night"),
    ("Walkable", "walkable"),
    ("Student budget", "student-budget"),
    ("Romantic", "romantic"),
    ("Wheelchair accessible", "wheelchair-accessible"),
    ("Vegetarian", "vegetarian"),
    ("Local favourite", "local-favourite"),
    ("Rainy day", "rainy-day"),
    ("Scenic", "scenic"),
    ("Craft", "craft"),
]

PUBLISHERS = [
    (
        "Addis Culture Bureau",
        "addis-culture-bureau",
        "public-sector",
        TRUST_LEVEL_STRATEGIC_PARTNER,
        "verified",
        0.94,
    ),
    (
        "National Museum of Ethiopia",
        "national-museum-ethiopia",
        "museum",
        TRUST_LEVEL_TRUSTED_ORG,
        "verified",
        0.91,
    ),
    (
        "Addis Ababa University",
        "addis-ababa-university",
        "education",
        TRUST_LEVEL_TRUSTED_ORG,
        "verified",
        0.88,
    ),
    (
        "Fendika Cultural Centre",
        "fendika-cultural-centre",
        "entertainment",
        TRUST_LEVEL_VERIFIED,
        "verified",
        0.86,
    ),
    ("Tomoca Coffee", "tomoca-coffee", "food-beverage", TRUST_LEVEL_VERIFIED, "verified", 0.89),
    ("Sheraton Addis", "sheraton-addis", "hospitality", TRUST_LEVEL_TRUSTED_ORG, "verified", 0.87),
    ("Zoma Museum", "zoma-museum", "museum", TRUST_LEVEL_VERIFIED, "verified", 0.85),
    (
        "Entoto Natural Park",
        "entoto-natural-park",
        "public-sector",
        TRUST_LEVEL_TRUSTED_ORG,
        "verified",
        0.82,
    ),
    (
        "Addis Running Collective",
        "addis-running-collective",
        "community",
        TRUST_LEVEL_VERIFIED,
        "verified",
        0.78,
    ),
    (
        "Alliance Ethio-Francaise",
        "alliance-ethio-francaise",
        "culture",
        TRUST_LEVEL_TRUSTED_ORG,
        "verified",
        0.84,
    ),
    ("Mesob Kitchen", "mesob-kitchen", "food-beverage", TRUST_LEVEL_VERIFIED, "verified", 0.80),
    (
        "Addis Tech Community",
        "addis-tech-community",
        "community",
        TRUST_LEVEL_VERIFIED,
        "pending",
        0.71,
    ),
]

# (name, slug, publisher_slug, neighborhood_slug, address, lat, lng, capacity,
#  facilities, accessibility)
VENUES = [
    (
        "National Museum of Ethiopia",
        "national-museum-ethiopia",
        "national-museum-ethiopia",
        "arat-kilo",
        "King George VI St, Arat Kilo",
        9.0384,
        38.7626,
        400,
        ["cafe", "gift shop", "guided tours"],
        {"wheelchairAccessible": True, "accessibleToilets": True},
    ),
    (
        "Fendika Cultural Centre",
        "fendika-cultural-centre",
        "fendika-cultural-centre",
        "kazanchis",
        "Zewditu St, Kazanchis",
        9.0104,
        38.7638,
        180,
        ["bar", "stage", "gallery"],
        {"wheelchairAccessible": False},
    ),
    (
        "Tomoca Coffee Piassa",
        "tomoca-piassa",
        "tomoca-coffee",
        "piassa",
        "Wawel St, Piassa",
        9.0346,
        38.7489,
        60,
        ["counter seating", "beans to buy"],
        {"wheelchairAccessible": False},
    ),
    (
        "Zoma Museum",
        "zoma-museum",
        "zoma-museum",
        "sarbet",
        "Mekanisa Rd, Sarbet",
        8.9905,
        38.7375,
        250,
        ["garden", "cafe", "library"],
        {"wheelchairAccessible": True},
    ),
    (
        "Entoto Park",
        "entoto-park",
        "entoto-natural-park",
        "entoto",
        "Entoto Ridge",
        9.0876,
        38.7620,
        5000,
        ["trails", "picnic areas", "bike hire", "parking"],
        {"wheelchairAccessible": True, "notes": "Main paths only"},
    ),
    (
        "Sheraton Addis Terrace",
        "sheraton-addis-terrace",
        "sheraton-addis",
        "kazanchis",
        "Taitu St, Kazanchis",
        9.0143,
        38.7635,
        300,
        ["restaurant", "bar", "valet"],
        {"wheelchairAccessible": True, "accessibleToilets": True},
    ),
    (
        "Alliance Ethio-Francaise",
        "alliance-ethio-francaise",
        "alliance-ethio-francaise",
        "arat-kilo",
        "Wavel St, Arat Kilo",
        9.0327,
        38.7639,
        220,
        ["auditorium", "library", "cafe"],
        {"wheelchairAccessible": True},
    ),
    (
        "AAU Sidist Kilo Campus",
        "aau-sidist-kilo",
        "addis-ababa-university",
        "arat-kilo",
        "Sidist Kilo",
        9.0409,
        38.7627,
        1200,
        ["lecture halls", "library", "wifi"],
        {"wheelchairAccessible": True},
    ),
    (
        "Merkato Spice Lanes",
        "merkato-spice-lanes",
        "addis-culture-bureau",
        "merkato",
        "Merkato Central",
        9.0335,
        38.7345,
        2000,
        ["market stalls"],
        {"wheelchairAccessible": False, "notes": "Crowded, uneven ground"},
    ),
    (
        "Mesob Kitchen Bole",
        "mesob-kitchen-bole",
        "mesob-kitchen",
        "bole",
        "Cameroon St, Bole",
        8.9962,
        38.7869,
        120,
        ["terrace", "traditional seating"],
        {"wheelchairAccessible": True},
    ),
    (
        "Meskel Square",
        "meskel-square",
        "addis-culture-bureau",
        "kazanchis",
        "Meskel Square",
        9.0107,
        38.7614,
        20000,
        ["open space", "steps"],
        {"wheelchairAccessible": True},
    ),
    (
        "Old Airport Garden Cafe",
        "old-airport-garden-cafe",
        "tomoca-coffee",
        "old-airport",
        "Ring Rd, Old Airport",
        8.9915,
        38.7256,
        80,
        ["garden", "wifi", "parking"],
        {"wheelchairAccessible": True},
    ),
    (
        "Ethnological Museum",
        "ethnological-museum",
        "addis-ababa-university",
        "arat-kilo",
        "AAU Main Campus, Sidist Kilo",
        9.0398,
        38.7638,
        300,
        ["gift shop", "guided tours"],
        {"wheelchairAccessible": False},
    ),
    (
        "Bole Medhanialem Area",
        "bole-medhanialem",
        "addis-culture-bureau",
        "bole",
        "Medhanialem, Bole",
        8.9998,
        38.7810,
        800,
        ["restaurants", "shops"],
        {"wheelchairAccessible": True},
    ),
]

# (title, slug, venue_slug, category_slug, type, summary, description, tags,
#  price_type, price, duration_min, indoor, quality, popularity, trend, rating, ratings)
EXPERIENCES = [
    (
        "Lucy and the Origins of Humankind",
        "lucy-origins-humankind",
        "national-museum-ethiopia",
        "arts-culture",
        TYPE_PLACE,
        "Stand in front of the 3.2-million-year-old Australopithecus skeleton.",
        "The National Museum's palaeontology hall holds Dinkinesh - known internationally as "
        "Lucy - alongside casts and finds from the Afar depression. The upper floors move "
        "through Aksumite artefacts and imperial regalia. Allow ninety minutes; the labelling "
        "rewards a slow walk, and the basement is cooler on a hot afternoon.",
        ["indoor", "family-friendly", "rainy-day", "wheelchair-accessible"],
        "fixed",
        150,
        90,
        True,
        0.93,
        0.88,
        0.42,
        4.6,
        214,
    ),
    (
        "Azmari Night at Fendika",
        "azmari-night-fendika",
        "fendika-cultural-centre",
        "music",
        TYPE_EVENT,
        "Improvised sung poetry, masinko fiddle and dancing that pulls in the room.",
        "Fendika's azmari evenings are the real thing: singers improvise verses about people "
        "in the room, the masinko carries the melody, and the eskista dancing starts whether "
        "you planned on it or not. Arrive by nine to get a seat near the front. Cash only at "
        "the bar.",
        ["live-music", "traditional", "late-night", "local-favourite"],
        "fixed",
        300,
        180,
        True,
        0.95,
        0.91,
        0.86,
        4.8,
        178,
    ),
    (
        "Tomoca Piassa Morning Cup",
        "tomoca-piassa-morning",
        "tomoca-piassa",
        "food-drink",
        TYPE_PLACE,
        "Standing-room espresso in a roastery that has been running since 1953.",
        "No chairs, no laptops, no ceremony - you order at the counter, drink standing at the "
        "wooden ledges, and leave. The macchiato is what most regulars come for. Buy beans on "
        "the way out; they roast on site and the queue moves faster than it looks.",
        ["coffee", "traditional", "walkable", "local-favourite", "indoor"],
        "fixed",
        60,
        25,
        True,
        0.90,
        0.94,
        0.55,
        4.7,
        402,
    ),
    (
        "Zoma Museum Gardens",
        "zoma-museum-gardens",
        "zoma-museum",
        "arts-culture",
        TYPE_PLACE,
        "Mud-and-straw architecture, permaculture gardens and contemporary art in one compound.",
        "Zoma is built entirely from local earth, its walls hand-sculpted with relief patterns. "
        "The gardens are a working permaculture project supplying the cafe, and the galleries "
        "rotate contemporary Ethiopian work. It is quiet in a way little else in the city is.",
        ["outdoor", "hidden-gem", "family-friendly", "scenic", "wheelchair-accessible"],
        "fixed",
        200,
        120,
        False,
        0.91,
        0.62,
        0.71,
        4.7,
        96,
    ),
    (
        "Entoto Ridge Sunrise Walk",
        "entoto-ridge-sunrise-walk",
        "entoto-park",
        "outdoors",
        TYPE_ACTIVITY,
        "Eucalyptus trails at 3,200m with the whole city laid out below.",
        "The ridge above Addis is cooler, thinner-aired and smells overwhelmingly of "
        "eucalyptus. Trails range from a gentle hour to a hard half-day. Go at sunrise for the "
        "light and to beat the weekend crowds. Bring a layer - it is genuinely cold before "
        "eight.",
        ["outdoor", "scenic", "free", "walkable"],
        "free",
        None,
        150,
        False,
        0.88,
        0.79,
        0.64,
        4.6,
        143,
    ),
    (
        "Merkato Spice Walk",
        "merkato-spice-walk",
        "merkato-spice-lanes",
        "markets",
        TYPE_ACTIVITY,
        "A guided route through the berbere and shiro lanes of the largest market in Africa.",
        "Merkato is overwhelming without a guide and rewarding with one. The route covers the "
        "spice lanes, the recycled-goods quarter where oil drums become stoves, and the coffee "
        "wholesalers. Two hours on foot. Leave valuables at the hotel and wear closed shoes.",
        ["traditional", "walkable", "local-favourite"],
        "fixed",
        450,
        120,
        False,
        0.84,
        0.73,
        0.58,
        4.4,
        87,
    ),
    (
        "Friday Jazz on the Terrace",
        "friday-jazz-terrace",
        "sheraton-addis-terrace",
        "music",
        TYPE_EVENT,
        "Ethio-jazz sets outdoors, with the fountains going and the city lit up below.",
        "The Sheraton's terrace sessions lean into the Mulatu Astatke lineage - vibraphone, "
        "sax, that unmistakable minor-key groove. Smart casual. Book a table if you want to "
        "eat; the bar is walk-in.",
        ["live-music", "romantic", "outdoor", "late-night"],
        "range",
        400,
        180,
        False,
        0.89,
        0.84,
        0.77,
        4.5,
        156,
    ),
    (
        "Ethiopian Coffee Ceremony",
        "ethiopian-coffee-ceremony",
        "mesob-kitchen-bole",
        "food-drink",
        TYPE_ACTIVITY,
        "Green beans roasted, ground and brewed in front of you across three servings.",
        "The ceremony takes about an hour and is not a performance for tourists - it is how "
        "coffee is served. Beans are roasted over coals, ground by hand, and brewed in a jebena "
        "for three rounds: abol, tona and baraka. Popcorn and incense throughout. Skipping the "
        "third round is considered rude.",
        ["coffee", "traditional", "family-friendly", "indoor", "vegetarian"],
        "fixed",
        250,
        60,
        True,
        0.92,
        0.81,
        0.60,
        4.8,
        231,
    ),
    (
        "Sidist Kilo Student Film Night",
        "sidist-kilo-film-night",
        "aau-sidist-kilo",
        "learning",
        TYPE_EVENT,
        "Free screenings of Ethiopian and pan-African cinema, with a discussion after.",
        "The AAU film society screens weekly, mostly Ethiopian and pan-African work, sometimes "
        "with the director present. Free and open to the public - bring ID for campus entry. "
        "The post-screening discussion is often the best part.",
        ["free", "student-budget", "indoor", "rainy-day"],
        "free",
        None,
        150,
        True,
        0.79,
        0.51,
        0.66,
        4.3,
        64,
    ),
    (
        "Francophone Film Festival",
        "francophone-film-festival",
        "alliance-ethio-francaise",
        "arts-culture",
        TYPE_EVENT,
        "A week of West and North African cinema, subtitled in English and Amharic.",
        "The Alliance's annual festival brings a strong programme of Francophone African "
        "cinema. Subtitles in English and Amharic. The courtyard bar runs between screenings "
        "and the closing night usually has live music.",
        ["indoor", "rainy-day", "wheelchair-accessible"],
        "fixed",
        100,
        120,
        True,
        0.86,
        0.68,
        0.81,
        4.4,
        72,
    ),
    (
        "Sunday Morning Run Club",
        "sunday-morning-run-club",
        "meskel-square",
        "sports",
        TYPE_EVENT,
        "Join the crowd that turns Meskel Square's steps into a stadium every Sunday.",
        "On Sunday mornings the square fills with hundreds of runners doing step repeats and "
        "loops - it is one of the sights of the city whether or not you run. The collective "
        "welcomes visitors; pace groups from easy to fast. Altitude is 2,350m, so start slower "
        "than you think.",
        ["free", "outdoor", "local-favourite", "family-friendly"],
        "free",
        None,
        90,
        False,
        0.85,
        0.77,
        0.69,
        4.7,
        118,
    ),
    (
        "Ethnological Museum in the Palace",
        "ethnological-museum-palace",
        "ethnological-museum",
        "arts-culture",
        TYPE_PLACE,
        "Haile Selassie's former palace, now a museum of Ethiopia's peoples.",
        "Housed in the emperor's old palace on the university campus, the collection is "
        "organised around the human life cycle across Ethiopia's nations and peoples. "
        "Selassie's bedroom and bathroom are preserved upstairs. The icon collection on the top "
        "floor is outstanding and usually empty.",
        ["indoor", "hidden-gem", "rainy-day", "traditional"],
        "fixed",
        200,
        90,
        True,
        0.89,
        0.58,
        0.45,
        4.5,
        88,
    ),
    (
        "Old Airport Garden Brunch",
        "old-airport-garden-brunch",
        "old-airport-garden-cafe",
        "food-drink",
        TYPE_PLACE,
        "Slow weekend mornings under jacaranda trees, away from the traffic.",
        "A walled garden in the diplomatic quarter with good coffee, proper breakfasts and "
        "enough shade to sit out the middle of the day. Wifi is reliable, which is why half the "
        "tables are working. Quietest before ten.",
        ["coffee", "outdoor", "family-friendly", "vegetarian", "wheelchair-accessible"],
        "range",
        220,
        90,
        False,
        0.83,
        0.66,
        0.38,
        4.4,
        137,
    ),
    (
        "Bole Night Food Crawl",
        "bole-night-food-crawl",
        "bole-medhanialem",
        "nightlife",
        TYPE_ACTIVITY,
        "Tibs, kitfo and draught beer across four stops in one walkable stretch.",
        "Bole after dark on foot: raw kitfo at a specialist, sizzling tibs at the next stop, "
        "then draught and shisha. Four stops, roughly three hours, all within fifteen minutes' "
        "walk. Vegetarian substitutions available at every stop if you ask.",
        ["late-night", "walkable", "local-favourite", "traditional"],
        "range",
        600,
        180,
        False,
        0.81,
        0.72,
        0.74,
        4.3,
        95,
    ),
    (
        "Addis Tech Community Meetup",
        "addis-tech-meetup",
        "aau-sidist-kilo",
        "learning",
        TYPE_EVENT,
        "Monthly talks on what people in the city are actually building.",
        "Two or three short talks, then an hour of standing around talking. Topics run from "
        "fintech and mobile money to machine learning. Free, no registration, and a genuinely "
        "good way to meet people if you have just arrived.",
        ["free", "student-budget", "indoor"],
        "free",
        None,
        120,
        True,
        0.76,
        0.48,
        0.72,
        4.2,
        51,
    ),
    (
        "Entoto Mountain Bike Descent",
        "entoto-mtb-descent",
        "entoto-park",
        "outdoors",
        TYPE_ACTIVITY,
        "Single-track from the ridge down through eucalyptus to the city edge.",
        "Bikes and helmets hire at the park gate. The descent is roughly forty minutes of "
        "flowing single-track, with a shuttle back up. Intermediate skills needed - loose "
        "gravel in the upper sections. Best in the dry season.",
        ["outdoor", "scenic"],
        "fixed",
        800,
        180,
        False,
        0.84,
        0.55,
        0.63,
        4.5,
        42,
    ),
    (
        "Shiro Cooking Class",
        "shiro-cooking-class",
        "mesob-kitchen-bole",
        "learning",
        TYPE_ACTIVITY,
        "Learn to build berbere from scratch, then cook shiro and injera with it.",
        "Starts at the spice market picking the components of berbere, then back to the kitchen "
        "to blend, toast and cook. You make shiro wat and eat it on injera you helped stretch. "
        "Recipes to take home. Fully vegetarian.",
        ["vegetarian", "traditional", "indoor", "family-friendly"],
        "fixed",
        950,
        240,
        True,
        0.87,
        0.49,
        0.57,
        4.7,
        38,
    ),
    (
        "Meskel Square Evening Walk",
        "meskel-square-evening-walk",
        "meskel-square",
        "outdoors",
        TYPE_PLACE,
        "The city's front room - best watched from the steps as the light goes.",
        "Meskel Square is where Addis gathers: runners at dawn, commuters all day, families in "
        "the evening. The steps on the north side are the place to sit. Free, always open, and "
        "the light at sunset over the eucalyptus is worth timing for.",
        ["free", "outdoor", "scenic", "walkable", "family-friendly", "wheelchair-accessible"],
        "free",
        None,
        45,
        False,
        0.78,
        0.83,
        0.40,
        4.2,
        167,
    ),
    (
        "Craft Market at Zoma",
        "craft-market-zoma",
        "zoma-museum",
        "markets",
        TYPE_EVENT,
        "Weekend market of ceramics, weaving and basketry from makers around the city.",
        "Held in the museum gardens, with about thirty makers - ceramics, hand-weaving, "
        "basketry, natural dyes. Prices are fixed and fair, and most stalls are the maker "
        "themselves. Good coffee on site.",
        ["craft", "outdoor", "family-friendly", "hidden-gem"],
        "free",
        None,
        120,
        False,
        0.82,
        0.57,
        0.68,
        4.5,
        61,
    ),
    (
        "Sunset Drinks Above Bole",
        "sunset-drinks-above-bole",
        "bole-medhanialem",
        "nightlife",
        TYPE_PLACE,
        "Rooftop bars along Medhanialem with the runway lights coming on.",
        "A run of rooftops looking west over Bole, timed for the sun going down behind the "
        "Entoto ridge. Drinks are mid-range, the crowd is a mix of returning diaspora and "
        "expats, and the planes on approach are part of the view.",
        ["romantic", "late-night", "scenic", "outdoor"],
        "range",
        350,
        120,
        False,
        0.80,
        0.75,
        0.66,
        4.3,
        109,
    ),
]

# When each recurring event actually happens, in Addis local time. Weekdays follow
# Python's Monday=0 convention; None means any day. Without this every event would
# be scheduled identically and the catalogue would read as obviously synthetic.
EVENT_SCHEDULE: dict[str, dict] = {
    "azmari-night-fendika": {"hour": 21, "minute": 0, "weekdays": {2, 3, 4, 5}},
    "friday-jazz-terrace": {"hour": 20, "minute": 30, "weekdays": {4}},
    "sidist-kilo-film-night": {"hour": 18, "minute": 30, "weekdays": {3}},
    "francophone-film-festival": {"hour": 19, "minute": 0, "weekdays": {1, 3, 5}},
    "sunday-morning-run-club": {"hour": 6, "minute": 30, "weekdays": {6}},
    "addis-tech-meetup": {"hour": 18, "minute": 0, "weekdays": {2}},
    "craft-market-zoma": {"hour": 10, "minute": 0, "weekdays": {5, 6}},
}

# Placeholder imagery grouped by category, so a live-music night is not
# illustrated with a photo of coffee beans. In production these are
# publisher-uploaded media (spec 54.03 s12); stock photography exists here only so
# the discovery surface can be evaluated with realistic-looking cards.
#
# Each subject below was confirmed by eye, not inferred from the photo id.
MEDIA_BY_CATEGORY: dict[str, list[str]] = {
    "food-drink": [
        "https://images.unsplash.com/photo-1447933601403-0c6688de566e",  # roasted coffee beans
        "https://images.unsplash.com/photo-1504674900247-0877df9cc836",  # plated dishes
        "https://images.unsplash.com/photo-1493857671505-72967e2e2760",  # cafe interior
        "https://images.unsplash.com/photo-1414235077428-338989a2e8c0",  # dish being served
    ],
    "music": [
        "https://images.unsplash.com/photo-1533174072545-7a4b6ad7a6c3",  # crowd, stage lights
        "https://images.unsplash.com/photo-1514933651103-005eec06c04b",  # bar interior at night
    ],
    "arts-culture": [
        "https://images.unsplash.com/photo-1499426600726-a950358acf16",  # museum courtyard
        "https://images.unsplash.com/photo-1552832230-c0197dd311b5",  # lit historic landmark
        "https://images.unsplash.com/photo-1523906834658-6e24ef2386f9",  # colonnaded architecture
    ],
    "outdoors": [
        "https://images.unsplash.com/photo-1470071459604-3b5ec3a7fe05",  # highland ridge at dusk
        "https://images.unsplash.com/photo-1523712999610-f77fbcfc3843",  # sunlit forest
    ],
    "nightlife": [
        "https://images.unsplash.com/photo-1514933651103-005eec06c04b",  # bar interior at night
        "https://images.unsplash.com/photo-1519671482749-fd09be7ccebf",  # drinks, evening toast
    ],
    "learning": [
        "https://images.unsplash.com/photo-1523240795612-9a054b0db644",  # people working together
    ],
    "markets": [
        "https://images.unsplash.com/photo-1533900298318-6b8da08a523e",  # market stalls
    ],
    "sports": [
        "https://images.unsplash.com/photo-1571008887538-b36bb32f4571",  # runner on the road
    ],
}

# Used when a category has no dedicated imagery.
MEDIA_FALLBACK = "https://images.unsplash.com/photo-1523712999610-f77fbcfc3843"  # sunlit forest


async def _upsert_city(session: AsyncSession) -> City:
    city = (
        await session.execute(select(City).where(City.slug == CITY["slug"]))
    ).scalar_one_or_none()
    if city is None:
        city = City(**CITY)
        session.add(city)
    else:
        for key, value in CITY.items():
            setattr(city, key, value)
    await session.flush()
    return city


async def _upsert_neighborhoods(session: AsyncSession, city: City) -> dict[str, Neighborhood]:
    existing = {
        n.slug: n
        for n in (
            await session.execute(select(Neighborhood).where(Neighborhood.city_id == city.id))
        ).scalars()
    }
    result: dict[str, Neighborhood] = {}
    for name, slug, description, lat, lng in NEIGHBORHOODS:
        neighborhood = existing.get(slug)
        if neighborhood is None:
            neighborhood = Neighborhood(city_id=city.id, slug=slug)
            session.add(neighborhood)
        neighborhood.name = name
        neighborhood.description = description
        neighborhood.latitude = lat
        neighborhood.longitude = lng
        result[slug] = neighborhood
    await session.flush()
    return result


async def _upsert_categories(session: AsyncSession) -> dict[str, Category]:
    existing = {c.slug: c for c in (await session.execute(select(Category))).scalars()}
    result: dict[str, Category] = {}
    for name, slug, icon, order in CATEGORIES:
        category = existing.get(slug)
        if category is None:
            category = Category(slug=slug)
            session.add(category)
        category.name = name
        category.icon = icon
        category.sort_order = order
        result[slug] = category
    await session.flush()
    return result


async def _upsert_tags(session: AsyncSession) -> dict[str, Tag]:
    existing = {t.slug: t for t in (await session.execute(select(Tag))).scalars()}
    result: dict[str, Tag] = {}
    for name, slug in TAGS:
        tag = existing.get(slug)
        if tag is None:
            tag = Tag(slug=slug)
            session.add(tag)
        tag.name = name
        result[slug] = tag
    await session.flush()
    return result


async def _upsert_publishers(session: AsyncSession) -> dict[str, Publisher]:
    existing = {p.slug: p for p in (await session.execute(select(Publisher))).scalars()}
    result: dict[str, Publisher] = {}
    for name, slug, industry, trust, verification, quality in PUBLISHERS:
        publisher = existing.get(slug)
        if publisher is None:
            publisher = Publisher(slug=slug)
            session.add(publisher)
        publisher.name = name
        publisher.industry = industry
        publisher.trust_level = trust
        publisher.verification_status = verification
        publisher.quality_score = quality
        publisher.description = f"{name} publishes experiences in Addis Ababa on Mado."
        result[slug] = publisher
    await session.flush()
    return result


async def _upsert_venues(
    session: AsyncSession,
    city: City,
    neighborhoods: dict[str, Neighborhood],
    publishers: dict[str, Publisher],
) -> dict[str, Venue]:
    existing = {v.slug: v for v in (await session.execute(select(Venue))).scalars()}
    result: dict[str, Venue] = {}
    for (
        name,
        slug,
        publisher_slug,
        neighborhood_slug,
        address,
        lat,
        lng,
        capacity,
        facilities,
        accessibility,
    ) in VENUES:
        venue = existing.get(slug)
        if venue is None:
            venue = Venue(slug=slug)
            session.add(venue)
        venue.name = name
        venue.city_id = city.id
        venue.publisher_id = publishers[publisher_slug].id
        venue.neighborhood_id = neighborhoods[neighborhood_slug].id
        venue.address = address
        venue.latitude = lat
        venue.longitude = lng
        venue.capacity = capacity
        venue.facilities = facilities
        venue.accessibility = accessibility
        venue.opening_hours = {
            "mon": "08:00-20:00",
            "tue": "08:00-20:00",
            "wed": "08:00-20:00",
            "thu": "08:00-20:00",
            "fri": "08:00-22:00",
            "sat": "09:00-22:00",
            "sun": "09:00-18:00",
        }
        result[slug] = venue
    await session.flush()
    return result


def _event_times(slug: str, experience_type: str, now: datetime) -> list[datetime]:
    """Generate occurrence times relative to the seed run.

    Anchored to "now" rather than to fixed dates, so the rails have content however
    long after seeding the app is first opened.

    Two things this gets deliberately right:

    * Times are computed in **Addis local time** and converted back to UTC. Doing
      the arithmetic in UTC would place a "19:30 evening event" at 22:30 local,
      outside the tonight window the API computes, leaving the rail empty.
    * Each event keeps its natural hour and day. Forcing an imminent occurrence on
      to everything would guarantee a full Tonight rail, but a Sunday morning run
      club starting at 23:26 on a Wednesday destroys the credibility of every other
      recommendation beside it.
    """
    if experience_type != TYPE_EVENT:
        return []

    schedule = EVENT_SCHEDULE.get(slug, {"hour": 19, "minute": 30, "weekdays": None})
    hour, minute = schedule["hour"], schedule["minute"]
    weekdays: set[int] | None = schedule["weekdays"]

    local_now = now.astimezone(CITY_TZ)
    times: list[datetime] = []

    # Walk forward a month and keep the slots that match this event's own pattern.
    for day_offset in range(0, 29):
        candidate = (local_now + timedelta(days=day_offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= local_now:
            continue
        if weekdays is not None and candidate.weekday() not in weekdays:
            continue
        times.append(candidate)
        if len(times) >= 5:
            break

    return [t.astimezone(UTC) for t in times]


async def seed(session: AsyncSession) -> dict[str, int]:
    """Populate the pilot city. Safe to run repeatedly."""
    now = datetime.now(UTC)

    city = await _upsert_city(session)
    neighborhoods = await _upsert_neighborhoods(session, city)
    categories = await _upsert_categories(session)
    tags = await _upsert_tags(session)
    publishers = await _upsert_publishers(session)
    venues = await _upsert_venues(session, city, neighborhoods, publishers)

    # media and events are touched below, so they must be eager-loaded: a lazy load
    # inside an async session raises MissingGreenlet rather than silently working.
    existing_experiences = {
        e.slug: e
        for e in (
            await session.execute(
                select(Experience)
                .where(Experience.city_id == city.id)
                .options(
                    selectinload(Experience.media),
                    selectinload(Experience.events),
                    selectinload(Experience.tags),
                )
            )
        ).scalars()
    }

    event_count = 0
    for index, (
        title,
        slug,
        venue_slug,
        category_slug,
        exp_type,
        summary,
        description,
        tag_slugs,
        price_type,
        price,
        duration,
        indoor,
        quality,
        # Consumed by _seed_interactions, not written to the row: these describe
        # the demand to simulate, and the learning loop derives the scores.
        _popularity,
        _trend,
        rating,
        rating_count,
    ) in enumerate(EXPERIENCES):
        venue = venues[venue_slug]
        experience = existing_experiences.get(slug)
        if experience is None:
            # Initialise the collections so later access is never a lazy load.
            experience = Experience(slug=slug, media=[], events=[], tags=[])
            session.add(experience)

        experience.title = title
        experience.summary = summary
        experience.description = description
        experience.type = exp_type
        experience.status = STATUS_PUBLISHED
        experience.published_at = now - timedelta(days=RNG.randint(3, 120))
        experience.city_id = city.id
        experience.venue_id = venue.id
        experience.publisher_id = venue.publisher_id
        experience.category_id = categories[category_slug].id
        experience.price_type = price_type
        experience.price_amount = price
        experience.price_max = (price * 2) if (price and price_type == "range") else None
        experience.currency = "ETB"
        experience.duration_minutes = duration
        experience.is_indoor = indoor
        experience.quality_score = quality
        # popularity_score and trend_score are deliberately NOT set here. They are
        # outputs of the learning loop, and writing them directly would mean the
        # Trending rail showed whatever this file asserted rather than what
        # explorers did. The seeded `popularity` and `trend` figures below are used
        # instead to generate a plausible interaction history, which the loop then
        # reads - so the demo exercises the real code path rather than bypassing it.
        experience.rating_average = rating
        experience.rating_count = rating_count
        experience.accessibility = venue.accessibility
        experience.attributes = {"seeded": True}
        experience.tags = [tags[t] for t in tag_slugs if t in tags]

        await session.flush()

        # Replace rather than skip, so a change to the imagery map takes effect on
        # the next seed instead of being masked by whatever ran first.
        for existing_media in list(experience.media or []):
            await session.delete(existing_media)
        await session.flush()

        pool = MEDIA_BY_CATEGORY.get(category_slug)
        base = pool[index % len(pool)] if pool else MEDIA_FALLBACK
        session.add(
            Media(
                experience_id=experience.id,
                type="image",
                url=f"{base}?w=1200&q=80&auto=format&fit=crop",
                alt_text=title,
                sort_order=0,
            )
        )

        # Rebuild occurrences each run so seeded events never go stale.
        for event in list(experience.events or []):
            await session.delete(event)
        await session.flush()

        for start in _event_times(slug, exp_type, now):
            session.add(
                EventInstance(
                    experience_id=experience.id,
                    start_time=start,
                    end_time=start + timedelta(minutes=duration or 120),
                    status="scheduled",
                    capacity=venue.capacity,
                    remaining=RNG.randint(5, venue.capacity or 50),
                )
            )
            event_count += 1

    await session.flush()

    interaction_count = await _seed_interactions(session, now=now)

    counts = {
        "cities": 1,
        "neighborhoods": len(neighborhoods),
        "categories": len(categories),
        "tags": len(tags),
        "publishers": len(publishers),
        "venues": len(venues),
        "experiences": len(EXPERIENCES),
        "events": event_count,
        "interactions": interaction_count,
    }
    logger.info("seed_complete", **counts)
    return counts


# Rough shape of a real engagement funnel: most people look, some open, few save.
# Used to turn a target volume into a realistic mix rather than a uniform one.
_FUNNEL = (
    ("view", 0.70),
    ("open_details", 0.20),
    ("save", 0.07),
    ("share", 0.02),
    ("dismiss", 0.01),
)

# Busiest listing gets this many interactions across the window. Small enough to
# seed quickly, large enough that the trend calculation has something to work with
# above its minimum-evidence floor.
_PEAK_INTERACTIONS = 90


async def _seed_interactions(session: AsyncSession, *, now: datetime) -> int:
    """Generate a plausible behavioural history for the seeded catalogue.

    Written as interaction *events*, not as aggregate scores, so the learning loop
    is what produces popularity and trend. That distinction matters beyond
    tidiness: it means the demo environment exercises the same path production
    does, and a bug in the aggregation shows up here instead of hiding behind
    hardcoded numbers.

    Events are attributed to synthetic anonymous ids, never to real accounts. A
    seeded account that appeared to have browsed ninety listings would corrupt that
    person's actual personalization with behaviour they never performed.
    """
    from app.domains.explorer.models import InteractionEvent

    # Rebuild each run so repeated seeding does not compound into a fake boom.
    await session.execute(
        delete(InteractionEvent).where(InteractionEvent.anonymous_id.like("seed-%"))
    )

    experiences = {
        experience.slug: experience
        for experience in (
            await session.execute(
                select(Experience).where(
                    Experience.attributes["seeded"].astext == "true"
                )
            )
        ).scalars()
    }

    window_days = 30
    trend_days = 7
    created = 0

    for row in EXPERIENCES:
        slug, popularity, trend = row[1], row[13], row[14]
        experience = experiences.get(slug)
        if experience is None:
            continue

        total = max(3, int(_PEAK_INTERACTIONS * float(popularity)))
        # A trending listing earns a disproportionate share of its activity in the
        # recent window - which is exactly the pattern the trend calculation looks
        # for, rather than simply having a lot of activity overall.
        #
        # The baseline sits just below the 7/30 share a flat distribution would
        # produce, so an untrending listing genuinely reads as untrending. Setting
        # it above that made every seeded listing look like it was taking off.
        recent_share = 0.20 + (0.35 * float(trend))

        for _ in range(total):
            if RNG.random() < recent_share:
                age_days = RNG.uniform(0, trend_days)
            else:
                age_days = RNG.uniform(trend_days, window_days)

            action = RNG.choices(
                [name for name, _ in _FUNNEL], weights=[w for _, w in _FUNNEL], k=1
            )[0]
            session.add(
                InteractionEvent(
                    anonymous_id=f"seed-{RNG.randint(1, 400):03d}",
                    action=action,
                    entity_type="experience",
                    entity_id=experience.id,
                    weight=1,
                    context={"seeded": True},
                    occurred_at=now - timedelta(days=age_days),
                )
            )
            created += 1

    await session.flush()
    return created

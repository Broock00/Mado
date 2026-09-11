"""Where somebody is, and where a place name points.

The questions the platform asks before it asks anything else. No answer is
stored: geography belongs to Google and OpenStreetMap, and Mado keeps its own
events, venues and explorers. A city or a street does not need a row here before
an explorer standing on it can be told where they are.

Four routes, in the order a search uses them:

* `/places/autocomplete` - while somebody types. Returns names and identifiers
  and deliberately no coordinates, because the provider has not been asked for
  any yet.
* `/places/details` - once they choose. Turns one identifier into a full place.
* `/places/search` - one query, whole, for a caller with nowhere to put a
  dropdown. The concierge is why it exists.
* `/places/resolve` - coordinates into a place, for "where am I".

**The split between the first two is what makes this affordable.** Google bills
autocomplete per request and details per lookup, but a `sessionToken` passed
through the keystrokes *and* the details call collapses the whole search into one
billed lookup. The token is minted by the client and forwarded untouched here -
the server does not invent one, because a token shared between two explorers
would bill their searches as one and return the wrong grouping to both.

All four are public. A visitor who has not signed in still gets a location
context, because "what is on near me" is the first thing the app does and putting
it behind an account would make the front page useless to everybody arriving for
the first time.

All four are rate limited, because a miss costs a request to somebody else's
service. Results are cached inside the provider, keyed on coordinates rounded to
about a hundred metres - fine enough for a street, coarse enough that the cache
cannot be read as a movement log. Autocomplete alone is never cached: predictions
belong to one session, and reusing them across sessions would break both the
billing grouping and the ranking that made them useful.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import Field

from app.api.deps import OptionalUser
from app.core import rate_limit
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import NotFoundError
from app.domains.catalog.localisation import currency_for_country
from app.domains.catalog.schemas import CamelModel
from app.integrations import places as places_module

router = APIRouter(tags=["places"])


class PlaceOut(CamelModel):
    """A place, in whatever detail the provider had.

    Almost everything is optional because the administrative chain differs by
    country: a London borough is not a state, and a country that has no counties
    should not report an empty one. Clients render what is there.
    """

    latitude: float
    longitude: float
    # The shortest phrase a person would use for this place.
    label: str
    # The wider area, for "near you in Brooklyn".
    area: str
    display_name: str = ""
    country: str | None = None
    country_code: str | None = None
    region: str | None = None
    county: str | None = None
    locality: str | None = None
    district: str | None = None
    neighbourhood: str | None = None
    road: str | None = None
    postcode: str | None = None
    kind: str | None = None
    # How far around this place it makes sense to look first. A street is a few
    # minutes' walk; a city is the whole evening.
    suggested_radius_km: float
    # south, west, north, east - present when the place has real extent that a
    # radius cannot represent (a borough, a region). Omitted for a country,
    # whose box is the wrong tool, and for a street, which is a point.
    bounding_box: list[float] | None = None
    provider: str
    # The provider's identifier for this place. Sent back so a client can save a
    # venue against it - the one part of a Google result that may be stored
    # indefinitely, and the only part that is.
    place_id: str | None = None
    # Credits the provider requires be shown wherever this place is displayed.
    # Usually empty; when it is not, dropping it is a licence breach.
    attributions: list[str] = Field(default_factory=list)
    # The currency in official use in this country, from CLDR.
    #
    # Not a fact about the place and not something any provider returned - it is
    # a lookup on `country_code`, which is already here. It rides along because
    # the composer needs it the moment a location is chosen, to price a post in
    # the money of wherever it is, and the alternative is a second copy of the
    # country-to-currency table living in the browser.
    currency: str = ""


class SuggestionOut(CamelModel):
    """One row of a location box, while somebody is still typing.

    No coordinates, on purpose. Resolving every suggestion to a point would cost
    a lookup per row for the several rows nobody picks; the client calls
    `/places/details` for the one that is chosen.
    """

    place_id: str
    text: str
    # Split so a dropdown can render "Brooklyn" above "NY, USA" rather than
    # eight rows that all begin the same way.
    primary: str = ""
    secondary: str = ""
    kinds: list[str] = Field(default_factory=list)
    # Metres from the coordinates the search was biased towards, when there were
    # any. What tells two identically-named streets apart.
    distance_metres: int | None = None
    provider: str


class LocationContextOut(CamelModel):
    """Everything the interface needs to say "here is where you are"."""

    resolved: bool
    place: PlaceOut | None = None
    # Present and false when the provider could not name the coordinates - the
    # middle of the sea, or a provider that is down. The interface says so
    # rather than showing a blank.
    reason: str | None = None


def _out(place: places_module.Place) -> PlaceOut:
    return PlaceOut(
        latitude=place.latitude,
        longitude=place.longitude,
        label=place.label,
        area=place.area_label,
        display_name=place.display_name,
        country=place.country,
        country_code=place.country_code,
        region=place.region,
        county=place.county,
        locality=place.locality,
        district=place.district,
        neighbourhood=place.neighbourhood,
        road=place.road,
        postcode=place.postcode,
        kind=place.kind,
        suggested_radius_km=place.suggested_radius_km,
        bounding_box=list(place.bounding_box) if place.bounding_box else None,
        provider=place.provider,
        place_id=place.place_id,
        attributions=list(place.attributions),
        currency=currency_for_country(place.country_code),
    )


def _suggestion_out(suggestion: places_module.Suggestion) -> SuggestionOut:
    return SuggestionOut(
        place_id=suggestion.place_id,
        text=suggestion.text,
        primary=suggestion.primary,
        secondary=suggestion.secondary,
        kinds=list(suggestion.kinds),
        distance_metres=suggestion.distance_metres,
        provider=suggestion.provider,
    )


@router.get(
    "/places/resolve",
    response_model=Envelope[LocationContextOut],
    summary="Where these coordinates are",
    description=(
        "Turns a latitude and longitude into a country, region, city, district, "
        "neighbourhood and street. Nothing is stored: the answer comes from a "
        "geocoding service every time, so an explorer standing somewhere Mado "
        "has never heard of still gets told where they are."
    ),
)
async def resolve(
    request: Request,
    _user: OptionalUser,
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
) -> Envelope[LocationContextOut]:
    await rate_limit.check(rate_limit.identify(request, None), rate_limit.PLACES_LIMIT)

    place = await places_module.get_provider().describe(lat, lng)
    if place is None:
        return Envelope(
            data=LocationContextOut(
                resolved=False,
                reason="We could not work out where that is.",
            )
        )
    return Envelope(data=LocationContextOut(resolved=True, place=_out(place)))


@router.get(
    "/places/search",
    response_model=CollectionEnvelope[PlaceOut],
    summary="Find a place by name",
    description=(
        "Resolves anything a person would type - a country, a city, a "
        "neighbourhood, a street, a landmark - without that place existing in "
        "Mado. Results are biased towards the explorer's own coordinates when "
        "they are supplied, so somebody in Manhattan typing \"Brooklyn\" gets "
        "the one next door, without excluding the rest of the world."
    ),
)
async def search(
    request: Request,
    _user: OptionalUser,
    q: str = Query(min_length=1, max_length=200),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    limit: int = Query(default=5, ge=1, le=10),
) -> CollectionEnvelope[PlaceOut]:
    await rate_limit.check(rate_limit.identify(request, None), rate_limit.PLACES_LIMIT)

    near = (lat, lng) if lat is not None and lng is not None else None
    found = await places_module.get_provider().search(q, near=near, limit=limit)
    return CollectionEnvelope(data=[_out(place) for place in found])


@router.get(
    "/places/autocomplete",
    response_model=CollectionEnvelope[SuggestionOut],
    summary="What somebody might be typing",
    description=(
        "Predictions for a partial query - a country, a city, a neighbourhood, "
        "an address, a landmark, a venue. Returns identifiers and names, never "
        "coordinates: call /places/details for the one that is chosen.\n\n"
        "Set `citiesOnly` when the question is which city rather than where "
        "exactly, and a street is not an answer.\n\n"
        "Pass the same `sessionToken` on every keystroke of one search and on "
        "the /places/details call that ends it. Doing so bills the whole search "
        "as a single lookup instead of one per keystroke. Mint a fresh token "
        "for the next search and never reuse one."
    ),
)
async def autocomplete(
    request: Request,
    _user: OptionalUser,
    q: str = Query(min_length=1, max_length=200),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    limit: int = Query(default=6, ge=1, le=10),
    session_token: str | None = Query(
        default=None,
        max_length=64,
        alias="sessionToken",
        description="Groups the keystrokes of one search with its details call.",
    ),
    cities_only: bool = Query(
        default=False,
        alias="citiesOnly",
        description="Narrow the predictions to inhabited places.",
    ),
) -> CollectionEnvelope[SuggestionOut]:
    await rate_limit.check(
        rate_limit.identify(request, None), rate_limit.PLACES_AUTOCOMPLETE_LIMIT
    )

    near = (lat, lng) if lat is not None and lng is not None else None
    found = await places_module.get_provider().autocomplete(
        q,
        near=near,
        limit=limit,
        session_token=session_token,
        cities_only=cities_only,
    )
    return CollectionEnvelope(data=[_suggestion_out(item) for item in found])


@router.get(
    "/places/details",
    response_model=Envelope[LocationContextOut],
    summary="Resolve a chosen suggestion",
    description=(
        "Turns an identifier from /places/autocomplete into a full place with "
        "coordinates and an administrative chain. Pass the same `sessionToken` "
        "used for the autocomplete calls that led here; it closes the session, "
        "and must not be used again afterwards."
    ),
)
async def details(
    request: Request,
    _user: OptionalUser,
    place_id: str = Query(
        min_length=1,
        max_length=512,
        alias="placeId",
        description="An identifier returned by /places/autocomplete.",
    ),
    session_token: str | None = Query(default=None, max_length=64, alias="sessionToken"),
) -> Envelope[LocationContextOut]:
    await rate_limit.check(rate_limit.identify(request, None), rate_limit.PLACES_LIMIT)

    place = await places_module.get_provider().details(
        place_id, session_token=session_token
    )
    if place is None:
        # 404 rather than a resolved:false envelope, which is what /places/resolve
        # returns for a point in the sea. That is a real answer about somewhere
        # unnameable; this is an identifier that does not resolve, which means the
        # client is holding something stale or from another provider - a
        # different problem, and one worth being able to see in the logs.
        raise NotFoundError("We could not find that place.", code="PLACE_NOT_FOUND")
    return Envelope(data=LocationContextOut(resolved=True, place=_out(place)))

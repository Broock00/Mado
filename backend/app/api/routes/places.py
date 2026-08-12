"""Where somebody is, and where a place name points.

The two questions the platform now asks before it asks anything else. Neither
answer is stored: geography belongs to OpenStreetMap and Google, and Mado keeps
its own events, venues and explorers. A city or a street does not need a row
here before an explorer standing on it can be told where they are.

Both routes are public. A visitor who has not signed in still gets a location
context, because "what is on near me" is the first thing the app does and
putting it behind an account would make the front page useless to everybody
arriving for the first time.

Both are rate limited, because a miss costs a request to somebody else's
service. Results are cached inside the provider, keyed on coordinates rounded to
about a hundred metres - fine enough for a street, coarse enough that the cache
cannot be read as a movement log.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.api.deps import OptionalUser
from app.core import rate_limit
from app.core.envelope import CollectionEnvelope, Envelope
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
        provider=place.provider,
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

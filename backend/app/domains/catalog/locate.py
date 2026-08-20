"""Turning a place somebody named into an area to search.

One resolution path, shared. The discovery route has always been able to answer
"what is on in Brooklyn" because the client sends a `place` parameter that gets
resolved here; the concierge could not, because its area came only from the
client - the device's position or whatever the place picker had been set to -
and never from the sentence.

So an explorer in Addis who typed "next week we are going to New York, is there
any music night there" was answered with Azmari Night in Kazanchis. Worse, the
substitution guard noticed and *said* so - "all the events I found are here, not
in New York" - which is honest about the failure and still a failure. The
question was answerable; nothing had asked New York.

Extracted rather than reimplemented because the rules here are subtle and were
learned the hard way:

* a **country** is scoped by its code, never its shape - Kenya's bounding box
  covers parts of four neighbours, and the United States' spans the globe
  because of the Pacific territories
* a **region** is not a circle either, so its box is used when it has one
* the **radius follows the kind of place**: a road is a short walk, a borough is
  not

Two of them living in two files would drift, and the failure would be quiet: the
concierge answering a slightly different question from the map beside it.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger
from app.domains.catalog.repository import Area
from app.integrations import places as places_module

logger = get_logger("mado.catalog.locate")


@dataclass(slots=True)
class ResolvedPlace:
    """Somewhere an explorer named, resolved to something searchable."""

    area: Area
    # What to call it back to them. "Brooklyn", not the city they are sitting in.
    label: str
    latitude: float
    longitude: float
    country_code: str | None = None
    # The provider's word for what this is: locality, country, route. Kept
    # because a caller sometimes needs to know it was a country rather than
    # re-deriving that from the area's shape.
    kind: str | None = None


async def resolve_place(
    name: str,
    *,
    near: tuple[float, float] | None = None,
    radius_km: float | None = None,
) -> ResolvedPlace | None:
    """Resolve a named place to an area, or None when nothing matches.

    `near` biases the search without restricting it, which is what makes "Bole"
    resolve to the neighbourhood in the explorer's own city rather than a street
    on another continent that happens to share the name. It must stay a bias: an
    explorer in Addis asking about New York has to be able to reach New York.
    """
    if not name or not name.strip():
        return None

    try:
        found = await places_module.get_provider().search(name.strip(), near=near, limit=1)
    except Exception as exc:  # noqa: BLE001 - a lookup failure must not fail the turn
        logger.warning("place_resolution_failed", place=name, error=str(exc))
        return None

    if not found:
        logger.info("place_not_resolved", place=name)
        return None

    target = found[0]
    is_country = (target.kind or "").lower() == "country"

    area = Area(
        latitude=target.latitude,
        longitude=target.longitude,
        # The place decides how wide to look: a road is a short walk and a
        # borough is not.
        radius_km=radius_km or target.suggested_radius_km,
        # A region is not a circle. Without its box, "Kenya" searches sixty
        # kilometres around the middle of the country - four hundred from
        # Nairobi - and finds nothing.
        bounding_box=(
            target.bounding_box if radius_km is None and not is_country else None
        ),
        country_code=target.country_code if is_country else None,
    )

    return ResolvedPlace(
        area=area,
        label=target.area_label or target.label,
        latitude=target.latitude,
        longitude=target.longitude,
        country_code=target.country_code,
        kind=target.kind,
    )

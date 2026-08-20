"""What money a place uses.

Needed for the same reason `app/integrations/timezones.py` is: a city row is now
materialised from wherever somebody drops a pin, and `cities.currency` has to be
filled for a country nobody anticipated. It used to be typed in alongside the ten
cities a developer had entered.

Read from CLDR through Babel rather than written out here. A hand-kept table of
250 countries looks stable and is not - currencies get redenominated, pegged,
replaced and joined to the euro, and the table would be wrong within a year in
exactly the places nobody is looking at.

This is not a fact about geography and does not belong with the place providers.
It is a fact about money, keyed by country, which is why it sits in the catalog
domain next to the city it fills in.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.logging import get_logger

logger = get_logger("mado.localisation")

# What a city gets when the country is unknown or has no currency in CLDR. ETB
# rather than USD because this platform's first city is Addis Ababa and a wrong
# default should at least be wrong in the direction of the people using it.
DEFAULT_CURRENCY = "ETB"


@lru_cache(maxsize=512)
def currency_for_country(country_code: str | None) -> str:
    """The currency currently in official use in a country.

    CLDR lists every currency a territory has *ever* used, each with the dates it
    was legal tender - Germany carries both DEM and EUR. The one wanted here is
    the one with no end date that is still tender, and taking the first entry
    instead would price a Berlin event in deutschmarks.
    """
    if not country_code:
        return DEFAULT_CURRENCY

    from babel.core import get_global

    entries = get_global("territory_currencies").get(country_code.upper(), ())
    # Each entry is (code, start, end, is_tender). Current means no end date;
    # tender excludes the accounting-only codes like USN that share a territory
    # with the real one.
    current = [code for code, _start, end, tender in entries if end is None and tender]
    if not current:
        logger.info("currency_unknown_for_country", country=country_code.upper())
        return DEFAULT_CURRENCY
    # Last rather than first: CLDR orders by the date use began, so the most
    # recently adopted is the one in circulation.
    return current[-1]

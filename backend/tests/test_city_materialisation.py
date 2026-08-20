"""A city is a consequence of somebody posting, not a precondition for it.

The platform could be used in exactly ten cities, because a `City` row had to
exist before a venue could point at one and a developer had to type it in. The
composer offered those ten in a dropdown. This is the machinery that replaced
that: a pin anywhere on earth resolves to a place, and the city row is
materialised from it.

The risks worth testing are not "does it create a row". They are the quiet ones:
a second venue in the same city making a second row, a materialised city
declaring itself launch-ready, and a country whose time zone cannot be
established being silently given the wrong one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

# Registers every mapper. Importing only the models this file names leaves
# relationships pointing at classes SQLAlchemy has not seen, and the mapper
# configuration fails on the first query with an error about `Publisher` that has
# nothing to do with cities.
import app.models  # noqa: F401
from app.domains.catalog import cities as module
from app.domains.catalog.localisation import DEFAULT_CURRENCY, currency_for_country
from app.domains.catalog.models import City
from app.integrations import timezones as timezones_module
from app.integrations.places import Place

pytestmark = pytest.mark.anyio


@pytest.fixture
async def session():
    """A real session, rolled back.

    A real one rather than a fake because the thing being tested *is* a query -
    "have we seen this city before" - and a fake would only prove the fake
    matches the test. Nothing here reaches the network: `city_for_place` is
    handed a resolved place, and the countries used resolve their time zone from
    CLDR offline.

    Rolled back rather than cleaned up afterwards, because cities have no owner
    and so are invisible to the teardown in conftest.py that deletes what the
    tests made. A leaked city row would sit in the database forever, and enough
    of them are how the concierge started recommending "Test Venue 0bf7".
    """
    from app.core.database import SessionFactory

    async with SessionFactory() as open_session:
        try:
            yield open_session
        finally:
            await open_session.rollback()


def place(**overrides) -> Place:
    """A resolved place, of the shape `describe` returns for a city."""
    fields = {
        "latitude": -1.2864,
        "longitude": 36.8172,
        "display_name": "Nairobi, Kenya",
        "locality": "Nairobi",
        "country": "Kenya",
        "country_code": "KE",
        "kind": "locality",
        "provider": "test",
    }
    fields.update(overrides)
    return Place(**fields)


class TestNamingACity:
    def test_the_locality_is_the_city(self):
        assert module.city_name_from(place()) == "Nairobi"

    def test_it_falls_back_down_the_administrative_chain(self):
        """A venue in open country, on an island, or somewhere whose chain skips
        the level a city would occupy still has to be postable - refusing it
        would put back the ceiling this removed, just higher up."""
        assert module.city_name_from(place(locality=None, district="Karen")) == "Karen"
        assert (
            module.city_name_from(place(locality=None, district=None, county="Kajiado"))
            == "Kajiado"
        )
        assert (
            module.city_name_from(
                place(locality=None, district=None, county=None, region=None)
            )
            == "Kenya"
        )

    def test_a_place_with_no_names_at_all_is_refused(self):
        """Middle of the ocean, or a provider that resolved nothing. Better to
        say so than to file a venue under "Unknown"."""
        assert (
            module.city_name_from(
                place(locality=None, district=None, county=None, region=None, country=None)
            )
            is None
        )


class TestMaterialisingACity:
    async def test_a_city_nobody_typed_in_is_created(self, session):
        city = await module.city_for_place(session, place(locality="Kisumu"))
        assert city is not None
        assert city.name == "Kisumu"
        assert city.country_code == "KE"

    async def test_it_is_not_declared_live(self, session):
        """`is_live` means the city passed the launch checklist in spec
        BUSINESS-08 - coverage, payments, moderation. Somebody adding a venue is
        not that judgement, and a row that claimed otherwise would put an
        unlaunched city into every "live cities" listing."""
        city = await module.city_for_place(session, place(locality="Eldoret"))
        assert city.is_live is False

    async def test_the_second_venue_in_a_city_joins_the_first(self, session):
        """The failure this guards is invisible until it matters: two Nairobi
        rows, each holding half the venues, and neither showing the whole city."""
        first = await module.city_for_place(session, place(locality="Mombasa"))
        second = await module.city_for_place(session, place(locality="Mombasa"))
        assert first.id == second.id
        count = await session.scalar(
            select(func.count()).select_from(City).where(City.name == "Mombasa")
        )
        assert count == 1

    async def test_matching_ignores_case(self, session):
        """Providers are not consistent about it, and "NAIROBI" and "Nairobi"
        are not two cities."""
        first = await module.city_for_place(session, place(locality="Nakuru"))
        second = await module.city_for_place(session, place(locality="nakuru"))
        assert first.id == second.id

    async def test_two_cities_of_the_same_name_in_different_countries_are_distinct(
        self, session
    ):
        """There is a Paris in France and a Paris in Texas, and somebody may post
        in either."""
        france = await module.city_for_place(
            session, place(locality="Springfield", country="France", country_code="FR")
        )
        states = await module.city_for_place(
            session,
            place(locality="Springfield", country="United States", country_code="US"),
        )
        assert france.id != states.id
        assert france.slug != states.slug

    async def test_the_currency_comes_from_the_country(self, session):
        city = await module.city_for_place(
            session, place(locality="Osaka", country="Japan", country_code="JP")
        )
        assert city.currency == "JPY"

    async def test_a_timezone_that_cannot_be_established_falls_back_to_utc(
        self, session, monkeypatch
    ):
        """Rather than to a guess. A city quietly given a neighbouring country's
        zone shows every event at the wrong time and looks entirely normal doing
        it - so the fallback is the one value that is obviously not an answer."""
        # Undone by monkeypatch at the end of the test. Not `reset_provider`,
        # which clears an lru_cache the replacement does not have.
        monkeypatch.setattr(
            timezones_module, "get_provider", lambda: timezones_module.StubTimeZones()
        )
        city = await module.city_for_place(
            session, place(locality="Denver", country="United States", country_code="US")
        )
        assert city.timezone == "UTC"

    async def test_a_single_zone_country_resolves_without_a_key(self, session):
        """Most countries have exactly one zone, and there the keyless answer is
        exactly as good as Google's."""
        city = await module.city_for_place(
            session, place(locality="Kisii", country="Kenya", country_code="KE")
        )
        assert city.timezone == "Africa/Nairobi"


class TestCurrencyByCountry:
    def test_a_country_that_changed_currency_reports_the_current_one(self):
        """CLDR lists every currency a territory has ever used, with the dates.
        Taking the first entry prices a Berlin event in deutschmarks."""
        assert currency_for_country("DE") == "EUR"

    def test_the_accounting_only_codes_are_not_offered(self):
        """The United States has USD, plus USN and USS which are not money
        anybody holds."""
        assert currency_for_country("US") == "USD"

    def test_an_unknown_country_gets_the_default_rather_than_an_error(self):
        assert currency_for_country("ZZ") == DEFAULT_CURRENCY
        assert currency_for_country(None) == DEFAULT_CURRENCY

    def test_the_seeded_cities_agree_with_cldr(self):
        """The ten cities somebody typed in by hand are the only independent
        check available that the lookup replacing them is right."""
        for country, currency in [
            ("ET", "ETB"),
            ("KE", "KES"),
            ("GB", "GBP"),
            ("JP", "JPY"),
            ("FR", "EUR"),
            ("MX", "MXN"),
            ("BR", "BRL"),
            ("IN", "INR"),
        ]:
            assert currency_for_country(country) == currency, country


class TestTerritoryTimeZones:
    async def test_it_declines_where_a_country_has_several_zones(self):
        """The United States has 29 and Brazil 16. Picking one would put a
        Denver event three hours out with nothing on screen to suggest why."""
        provider = timezones_module.TerritoryTimeZones()
        assert await provider.zone_for(39.7, -104.9, country_code="US") is None
        assert await provider.zone_for(-23.5, -46.6, country_code="BR") is None

    async def test_it_answers_where_a_country_has_one(self):
        provider = timezones_module.TerritoryTimeZones()
        assert await provider.zone_for(51.5, -0.1, country_code="GB") == "Europe/London"
        assert await provider.zone_for(9.0, 38.7, country_code="ET") == "Africa/Addis_Ababa"

    async def test_it_answers_nothing_without_a_country(self):
        provider = timezones_module.TerritoryTimeZones()
        assert await provider.zone_for(51.5, -0.1) is None

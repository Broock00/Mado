"""Tickets for the seeded catalogue.

Written because of what the fixture looked like without it: forty-seven paid
listings, a price on every one, and nothing anywhere in `commerce.ticket_types`.
The checkout, both provider adapters and the whole order flow were built and
none of it could be reached from the catalogue, so a paid listing showed a price
and offered a free reservation instead - which reads as a broken checkout rather
than as missing fixture data.

The pure shaping rules are tested here. Whether the rows land correctly is
covered by the seed itself running in CI.
"""

from __future__ import annotations

from app.seed.ticketing import tiers_for


class TestWhatAPriceShouldSell:
    def test_a_free_listing_sells_nothing(self):
        """Open-door things stay open-door. The model does treat "registration
        required" as a zero-priced ticket, but turning a park into a booking is
        a product decision nobody made."""
        assert tiers_for("free", None) == []
        assert tiers_for("free", 0) == []

    def test_a_listing_with_no_price_sells_nothing(self):
        """price_type says paid and price_amount is empty - there is no number
        to charge, and inventing one is worse than showing no tier."""
        assert tiers_for("fixed", None) == []
        assert tiers_for("fixed", 0) == []

    def test_a_fixed_price_sells_one_tier_at_that_price(self):
        tiers = tiers_for("fixed", 25)
        assert len(tiers) == 1
        assert tiers[0].price_major == 25

    def test_a_range_sells_two_tiers_at_different_prices(self):
        """A range means a cheaper way in and a better seat. Two tiers at the
        same price would render as a fixed price and contradict the listing's
        own price shape."""
        tiers = tiers_for("range", 10)
        assert len(tiers) == 2
        assert len({tier.price_major for tier in tiers}) == 2
        assert min(tier.price_major for tier in tiers) == 10

    def test_tiers_have_distinct_positions(self):
        """Two tiers sharing a position order arbitrarily, so the panel would
        list them differently between requests."""
        tiers = tiers_for("range", 10)
        assert len({tier.position for tier in tiers}) == len(tiers)

    def test_shares_do_not_oversell_the_room(self):
        """The shares divide one occupancy. Summing above 1.0 would print more
        tickets than the room holds, which the capacity check would then have
        to catch at the door."""
        for price_type in ("fixed", "range"):
            assert sum(tier.share for tier in tiers_for(price_type, 20)) <= 1.0

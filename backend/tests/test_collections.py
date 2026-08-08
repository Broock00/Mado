"""Collection tests.

Two things carry the risk here. Visibility, because the failure mode is showing
somebody's private list to strangers. And reorder, because it is the one piece
of real logic - everything else is CRUD that fails loudly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

# Constructing a real Collection configures SQLAlchemy's mappers, and that
# resolves relationships by name across every domain. Importing the registry the
# application uses is what makes `Venue.publisher` resolvable here.
import app.models  # noqa: F401
from app.core.errors import ValidationError
from app.domains.explorer.collections import (
    MAX_COLLECTIONS,
    MAX_ITEMS,
    VISIBILITIES,
    CollectionService,
    assert_can_publish,
    slugify,
)
from app.domains.explorer.models import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    VISIBILITY_UNLISTED,
    Collection,
    CollectionItem,
)

pytestmark = pytest.mark.anyio


def item(experience_id: uuid.UUID, position: int) -> CollectionItem:
    """A real mapped row, detached. A relationship will not accept a stand-in."""
    return CollectionItem(experience_id=experience_id, position=position, note=None)


def collection(**overrides) -> Collection:
    """A detached Collection. Nothing here touches the database."""
    row = Collection(
        user_id=overrides.pop("user_id", uuid.uuid4()),
        title=overrides.pop("title", "Best coffee"),
        slug=overrides.pop("slug", "best-coffee-abc123"),
        visibility=overrides.pop("visibility", VISIBILITY_PRIVATE),
        items=overrides.pop("items", []),
    )
    row.moderation_status = overrides.pop("moderation_status", "approved")
    row.deleted_at = overrides.pop("deleted_at", None)
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class TestSlugs:
    def test_a_title_becomes_a_readable_stem(self):
        assert slugify("Best Coffee in Addis").startswith("best-coffee-in-addis-")

    def test_punctuation_and_spacing_collapse(self):
        assert slugify("  Rainy--day   IDEAS!!  ").startswith("rainy-day-ideas-")

    def test_two_identical_titles_do_not_collide(self):
        """Not an edge case. Two people naming a list "Hidden Gems" is expected."""
        assert slugify("Hidden Gems") != slugify("Hidden Gems")

    def test_an_amharic_title_still_produces_a_usable_slug(self):
        """It strips to nothing, so the suffix carries it.

        Refusing the title would be refusing someone their own language, in the
        pilot city's own script. An opaque URL is the far smaller cost.
        """
        slug = slugify("የቡና ቤቶች")
        assert slug and slug.isascii() and not slug.startswith("-")

    def test_a_long_title_does_not_produce_an_unbounded_slug(self):
        assert len(slugify("word " * 200)) <= 100


class TestVisibility:
    def test_a_private_collection_is_neither_shareable_nor_listed(self):
        row = collection(visibility=VISIBILITY_PRIVATE)
        assert not row.is_shareable
        assert not row.is_discoverable

    def test_an_unlisted_collection_is_shareable_but_not_listed(self):
        """This is what "share" usually means - sending one person a link.

        Requiring moderator approval before you could text your coffee list to a
        friend would be absurd, so unlisted is reachable and invisible.
        """
        row = collection(visibility=VISIBILITY_UNLISTED)
        assert row.is_shareable
        assert not row.is_discoverable

    def test_a_public_collection_is_both(self):
        row = collection(visibility=VISIBILITY_PUBLIC)
        assert row.is_shareable
        assert row.is_discoverable

    def test_a_flagged_public_collection_is_withheld_from_the_directory(self):
        """Screening withholds promotion, not possession.

        The owner shared the link deliberately; what is in question is whether
        the platform should put it in front of people who did not ask.
        """
        row = collection(visibility=VISIBILITY_PUBLIC, moderation_status="flagged")
        assert row.is_shareable
        assert not row.is_discoverable

    def test_a_rejected_collection_is_not_even_shareable(self):
        row = collection(visibility=VISIBILITY_PUBLIC, moderation_status="rejected")
        assert not row.is_shareable

    def test_a_deleted_collection_is_gone_from_both(self):
        from datetime import UTC, datetime

        row = collection(visibility=VISIBILITY_PUBLIC, deleted_at=datetime.now(UTC))
        assert not row.is_shareable
        assert not row.is_discoverable

    def test_the_three_levels_are_the_only_ones(self):
        assert {VISIBILITY_PRIVATE, VISIBILITY_UNLISTED, VISIBILITY_PUBLIC} == VISIBILITIES


class TestPublishing:
    def test_an_empty_collection_cannot_be_shared(self):
        """An empty public page is broken for whoever follows the link."""
        with pytest.raises(ValidationError) as caught:
            assert_can_publish(collection(items=[]))
        assert caught.value.code == "COLLECTION_EMPTY"

    def test_one_item_is_enough(self):
        # Passes by not raising. There is no return value to assert on.
        assert_can_publish(collection(items=[item(uuid.uuid4(), 0)]))


class TestReorder:
    """Reorder takes the whole list, not a move.

    Two tabs each nudging one item converge on a coherent order this way and
    diverge with pairwise swaps.
    """

    async def service_over(self, row: Collection) -> CollectionService:
        service = CollectionService(session=None)  # type: ignore[arg-type]

        async def _owned(_user, _collection_id):
            return row

        service._owned = _owned  # type: ignore[method-assign]
        return service

    async def test_the_given_order_is_applied(self):
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        row = collection(items=[item(a, 0), item(b, 1), item(c, 2)])
        service = await self.service_over(row)

        await service.reorder(SimpleNamespace(id=row.user_id), row.id, [c, a, b])
        assert [i.position for i in row.items] == [1, 2, 0]

    async def test_an_omitted_item_is_kept_rather_than_dropped(self):
        """A stale client must not silently discard something the curator added.

        The tab that reorders was loaded before the third item existed; sending
        two ids should not mean "delete the one you did not hear about".
        """
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        row = collection(items=[item(a, 0), item(b, 1), item(c, 2)])
        service = await self.service_over(row)

        await service.reorder(SimpleNamespace(id=row.user_id), row.id, [b, a])
        positions = {i.experience_id: i.position for i in row.items}
        assert positions[b] == 0
        assert positions[a] == 1
        # Still present, and pushed to the end rather than removed.
        assert positions[c] == 2

    async def test_unknown_ids_are_ignored_rather_than_erroring(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        row = collection(items=[item(a, 0), item(b, 1)])
        service = await self.service_over(row)

        await service.reorder(SimpleNamespace(id=row.user_id), row.id, [uuid.uuid4(), b, a])
        assert {i.experience_id: i.position for i in row.items} == {b: 0, a: 1}

    async def test_positions_stay_contiguous_from_zero(self):
        ids = [uuid.uuid4() for _ in range(5)]
        row = collection(items=[item(i, n * 10) for n, i in enumerate(ids)])
        service = await self.service_over(row)

        await service.reorder(SimpleNamespace(id=row.user_id), row.id, list(reversed(ids)))
        assert sorted(i.position for i in row.items) == [0, 1, 2, 3, 4]


class TestLimits:
    def test_a_collection_is_capped_below_the_point_of_being_themed(self):
        """Two hundred coffee shops is not a theme, it is a directory."""
        assert 20 <= MAX_ITEMS <= 500

    def test_the_per_explorer_cap_is_an_abuse_ceiling_not_a_taste_one(self):
        assert MAX_COLLECTIONS >= 50


class TestCollectionsAreNotItineraries:
    def test_position_carries_no_timing(self):
        """The taxonomy distinction, asserted rather than trusted to comments.

        An itinerary claims you could do all of it in the order given. A
        collection claims nothing of the sort, so its items have a display
        position and no time fields at all - if one ever appears here, the two
        concepts have started merging.
        """
        from app.domains.explorer.models import CollectionItem

        columns = set(CollectionItem.__table__.c.keys())
        assert "position" in columns
        assert not columns & {"starts_at", "ends_at", "arrival_time", "duration_minutes"}

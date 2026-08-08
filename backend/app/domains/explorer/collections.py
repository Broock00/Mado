"""Collections: themed sets of experiences (spec EXP-003).

"Allow explorers to save, organize, and share themed collections."

Three decisions shape everything here.

**A collection is not a saved list.** Saving is a reflex - you see something,
you keep it, you move on. Curating is deliberate: you decide a thing belongs
with other things and you can say why. The two want different affordances, so
`SavedItem` stays the fast inbox and collections are what you make out of it.
Forcing them into one table would mean every save prompting "which list?", which
is exactly the friction that stops people saving.

**Sharing does not mean publishing.** Sending a friend your coffee list should
not queue for moderation. An unlisted collection is reachable by anyone with the
link and listed nowhere; only making one *public* - asking the platform to show
it to strangers - runs it past a screener. Anything else would either moderate
private notes or put unscreened pages in a public directory.

**Screening reads what is public, and nothing else.** A private collection is
never screened. Reading someone's private notes to check them against policy is
not moderation.
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domains.catalog.models import Experience
from app.domains.catalog.repository import with_card_relations
from app.domains.explorer.models import (
    SOURCE_USER,
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    VISIBILITY_UNLISTED,
    Collection,
    CollectionItem,
)
from app.domains.identity.models import User

logger = get_logger("mado.collections")

VISIBILITIES = {VISIBILITY_PRIVATE, VISIBILITY_UNLISTED, VISIBILITY_PUBLIC}

MAX_TITLE = 200
MAX_DESCRIPTION = 2000
MAX_NOTE = 500

# A ceiling on items, not because storage is scarce but because a "themed
# collection" with four hundred entries has stopped being themed. High enough
# that nobody legitimate meets it.
MAX_ITEMS = 200

# And on collections per explorer, which is the abuse ceiling rather than the
# taste one.
MAX_COLLECTIONS = 100

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    """A URL-safe stem, with a random suffix.

    The suffix is not decoration. Two people naming a collection "Hidden Gems"
    is not a collision to resolve, it is the expected case, and probing
    `/collections/hidden-gems-2` to see what exists is a small enumeration
    surface. Random suffixes make both problems go away.

    Non-Latin titles - Amharic, which this platform will see a lot of - strip to
    nothing here. Those fall back to the suffix alone rather than being rejected:
    an unreadable URL is a far smaller cost than refusing someone their own
    language.
    """
    stem = _SLUG_STRIP.sub("-", title.strip().lower()).strip("-")[:80]
    suffix = secrets.token_urlsafe(6).lower().replace("_", "").replace("-", "")[:8]
    return f"{stem}-{suffix}" if stem else suffix


@dataclass(slots=True)
class CollectionSummary:
    """A collection without its contents, for listings."""

    collection: Collection
    item_count: int
    # A few images from the items, so a card has something to show. Read from
    # the experiences rather than stored, because a cover that silently points
    # at a deleted listing is worse than no cover.
    preview_image_urls: list[str]


class CollectionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------- reading

    async def mine(self, user: User) -> list[CollectionSummary]:
        result = await self.session.execute(
            select(Collection)
            .where(Collection.user_id == user.id, Collection.deleted_at.is_(None))
            .order_by(Collection.updated_at.desc())
        )
        return await self._summarise(list(result.scalars().unique()))

    async def public(
        self, *, city_slug: str | None = None, limit: int = 30
    ) -> list[CollectionSummary]:
        """Collections anyone may browse.

        Only approved and public. A collection awaiting review stays reachable
        by its link but is not put in front of people who did not ask for it.
        """
        stmt = select(Collection).where(
            Collection.deleted_at.is_(None),
            Collection.visibility == VISIBILITY_PUBLIC,
            Collection.moderation_status == "approved",
        )
        if city_slug:
            stmt = stmt.where(Collection.city_slug == city_slug)

        result = await self.session.execute(
            stmt.order_by(Collection.updated_at.desc()).limit(limit)
        )
        return await self._summarise(list(result.scalars().unique()))

    async def get(self, collection_id: uuid.UUID, *, viewer: User | None) -> Collection:
        """One collection, if this viewer is allowed to see it.

        A private collection answers 404 rather than 403 for anyone but its
        owner. Telling a stranger "this exists but is not yours" leaks that it
        exists at all, which is the one thing private is supposed to prevent.
        """
        collection = await self._load(collection_id)

        if viewer is not None and collection.user_id == viewer.id:
            return collection
        if collection.is_shareable:
            return collection
        raise NotFoundError("Collection not found.", code="COLLECTION_NOT_FOUND")

    async def by_slug(self, slug: str, *, viewer: User | None) -> Collection:
        result = await self.session.execute(
            select(Collection)
            .where(Collection.slug == slug, Collection.deleted_at.is_(None))
            .options(selectinload(Collection.items))
        )
        collection = result.scalars().first()
        if collection is None:
            raise NotFoundError("Collection not found.", code="COLLECTION_NOT_FOUND")
        return await self.get(collection.id, viewer=viewer)

    async def experiences_in(self, collection: Collection) -> list[Experience]:
        """The listings themselves, in the collection's display order.

        Anything unpublished or withheld is dropped. A curated list must not
        become a back door to content moderation took down - and the curator
        did nothing wrong, so the rest of their list still stands.
        """
        if not collection.items:
            return []

        order = {item.experience_id: item.position for item in collection.items}
        # The catalogue's own loader rather than a hand-written list. Assembling
        # one here means missing `venue.neighborhood`, which is a MissingGreenlet
        # at request time rather than a slow query, and the traceback contains no
        # application frame to point at it.
        result = await self.session.execute(
            with_card_relations(
                select(Experience).where(
                    Experience.id.in_(order), Experience.deleted_at.is_(None)
                )
            )
        )
        found = [e for e in result.scalars().unique() if e.is_discoverable]
        return sorted(found, key=lambda e: order.get(e.id, 0))

    # ------------------------------------------------------------- writing

    async def create(
        self,
        user: User,
        *,
        title: str,
        description: str | None = None,
        city_slug: str | None = None,
        visibility: str = VISIBILITY_PRIVATE,
    ) -> Collection:
        title = (title or "").strip()
        if not title:
            raise ValidationError("Give the collection a title.", code="TITLE_REQUIRED")
        if visibility not in VISIBILITIES:
            raise ValidationError("Unknown visibility.", code="INVALID_VISIBILITY")

        existing = await self.session.scalar(
            select(func.count(Collection.id)).where(
                Collection.user_id == user.id, Collection.deleted_at.is_(None)
            )
        )
        if (existing or 0) >= MAX_COLLECTIONS:
            raise ConflictError(
                f"You have reached {MAX_COLLECTIONS} collections. Delete one to make another.",
                code="TOO_MANY_COLLECTIONS",
            )

        collection = Collection(
            user_id=user.id,
            title=title[:MAX_TITLE],
            slug=slugify(title),
            description=(description or "").strip()[:MAX_DESCRIPTION] or None,
            city_slug=city_slug,
            visibility=visibility,
            source=SOURCE_USER,
            # Stated explicitly, not left to default. Without it the first read
            # of `.items` on the flushed row is a lazy load, which raises
            # MissingGreenlet under asyncio - and a brand-new collection is
            # empty by definition, so there is nothing to go and fetch.
            items=[],
        )
        self.session.add(collection)
        await self.session.flush()
        logger.info("collection_created", collection_id=str(collection.id), visibility=visibility)
        return collection

    async def update(
        self,
        user: User,
        collection_id: uuid.UUID,
        *,
        title: str | None = None,
        description: str | None = None,
        visibility: str | None = None,
    ) -> Collection:
        collection = await self._owned(user, collection_id)

        if title is not None:
            cleaned = title.strip()
            if not cleaned:
                raise ValidationError("Give the collection a title.", code="TITLE_REQUIRED")
            # The slug deliberately does not follow. A link somebody already
            # shared has to keep working, and renaming a list is not a reason to
            # break it.
            collection.title = cleaned[:MAX_TITLE]

        if description is not None:
            collection.description = description.strip()[:MAX_DESCRIPTION] or None

        if visibility is not None:
            if visibility not in VISIBILITIES:
                raise ValidationError("Unknown visibility.", code="INVALID_VISIBILITY")
            collection.visibility = visibility

        return collection

    async def delete(self, user: User, collection_id: uuid.UUID) -> None:
        from datetime import UTC, datetime

        collection = await self._owned(user, collection_id)
        collection.deleted_at = datetime.now(UTC)
        logger.info("collection_deleted", collection_id=str(collection.id))

    # --------------------------------------------------------------- items

    async def add(
        self,
        user: User,
        collection_id: uuid.UUID,
        experience_id: uuid.UUID,
        *,
        note: str | None = None,
    ) -> CollectionItem:
        collection = await self._owned(user, collection_id)

        experience = await self.session.get(Experience, experience_id)
        if experience is None or experience.deleted_at is not None:
            raise NotFoundError("Experience not found.", code="EXPERIENCE_NOT_FOUND")

        if any(item.experience_id == experience_id for item in collection.items):
            raise ConflictError("That is already in this collection.", code="ALREADY_IN_COLLECTION")
        if len(collection.items) >= MAX_ITEMS:
            raise ConflictError(
                f"A collection holds at most {MAX_ITEMS} experiences.", code="COLLECTION_FULL"
            )

        item = CollectionItem(
            collection_id=collection.id,
            experience_id=experience_id,
            # Appended. New arrivals go to the end rather than the top, because
            # a curated order is the curator's, and reordering it on their
            # behalf every time they add something is not curation.
            position=max((i.position for i in collection.items), default=-1) + 1,
            note=(note or "").strip()[:MAX_NOTE] or None,
        )
        # Appended to the relationship rather than added to the session. Both
        # persist the row, but only this keeps the in-memory collection correct
        # - otherwise the response rendered from `collection.items` is missing
        # the item that was just added.
        collection.items.append(item)
        await self.session.flush()
        return item

    async def remove(self, user: User, collection_id: uuid.UUID, experience_id: uuid.UUID) -> None:
        collection = await self._owned(user, collection_id)
        item = next((i for i in collection.items if i.experience_id == experience_id), None)
        if item is None:
            raise NotFoundError("That is not in this collection.", code="NOT_IN_COLLECTION")
        # Removed from the relationship, so delete-orphan handles the row and
        # the in-memory list matches what the caller will be shown.
        collection.items.remove(item)

    async def annotate(
        self,
        user: User,
        collection_id: uuid.UUID,
        experience_id: uuid.UUID,
        note: str | None,
    ) -> CollectionItem:
        collection = await self._owned(user, collection_id)
        item = next((i for i in collection.items if i.experience_id == experience_id), None)
        if item is None:
            raise NotFoundError("That is not in this collection.", code="NOT_IN_COLLECTION")
        item.note = (note or "").strip()[:MAX_NOTE] or None
        return item

    async def reorder(
        self, user: User, collection_id: uuid.UUID, experience_ids: list[uuid.UUID]
    ) -> Collection:
        """Set display order from a full list of ids.

        A whole list rather than a move operation: two tabs each nudging one
        item produce a coherent result this way, and an incoherent one with
        pairwise swaps.

        Ids not in the collection are ignored, and items the caller omitted keep
        their relative order at the end - a stale client must not silently drop
        something the explorer curated.
        """
        collection = await self._owned(user, collection_id)
        by_experience = {item.experience_id: item for item in collection.items}

        position = 0
        for experience_id in experience_ids:
            item = by_experience.pop(experience_id, None)
            if item is None:
                continue
            item.position = position
            position += 1

        for item in sorted(by_experience.values(), key=lambda i: i.position):
            item.position = position
            position += 1

        return collection

    # ----------------------------------------------------------- internals

    async def _load(self, collection_id: uuid.UUID) -> Collection:
        result = await self.session.execute(
            select(Collection)
            .where(Collection.id == collection_id, Collection.deleted_at.is_(None))
            .options(selectinload(Collection.items))
        )
        collection = result.scalars().first()
        if collection is None:
            raise NotFoundError("Collection not found.", code="COLLECTION_NOT_FOUND")
        return collection

    async def _owned(self, user: User, collection_id: uuid.UUID) -> Collection:
        collection = await self._load(collection_id)
        if collection.user_id != user.id:
            # 404 rather than 403, for the same reason `get` does: a stranger
            # should not be able to tell someone else's collection apart from
            # one that does not exist.
            raise NotFoundError("Collection not found.", code="COLLECTION_NOT_FOUND")
        return collection

    async def _summarise(self, collections: list[Collection]) -> list[CollectionSummary]:
        if not collections:
            return []

        # One query for every preview rather than one per collection.
        experience_ids = {item.experience_id for c in collections for item in c.items}
        images: dict[uuid.UUID, str] = {}
        if experience_ids:
            result = await self.session.execute(
                select(Experience)
                .where(Experience.id.in_(experience_ids))
                .options(selectinload(Experience.media))
            )
            for experience in result.scalars().unique():
                media = sorted(experience.media, key=lambda m: m.sort_order)
                if media:
                    images[experience.id] = media[0].url

        summaries = []
        for collection in collections:
            ordered = sorted(collection.items, key=lambda i: i.position)
            previews = [images[i.experience_id] for i in ordered if i.experience_id in images]
            summaries.append(
                CollectionSummary(
                    collection=collection,
                    item_count=len(collection.items),
                    preview_image_urls=previews[:4],
                )
            )
        return summaries


def assert_can_publish(collection: Collection) -> None:
    """Refuse to make an empty collection public.

    Not a technical constraint - an empty public collection is simply a broken
    page for whoever follows the link, and the person best placed to notice is
    the one clicking the button.
    """
    if not collection.items:
        raise ValidationError(
            "Add something to the collection before sharing it.", code="COLLECTION_EMPTY"
        )


__all__ = [
    "MAX_COLLECTIONS",
    "MAX_ITEMS",
    "VISIBILITIES",
    "CollectionService",
    "CollectionSummary",
    "assert_can_publish",
    "slugify",
]

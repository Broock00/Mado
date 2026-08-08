"""Collections (spec EXP-003).

Save, organize and share themed sets of experiences.

Note the split between `/collections/mine` and `/collections/{id}`: the first
needs an account, the second does not. A shared link has to work for someone who
has never signed in, or it is not a shared link.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, status
from pydantic import Field

from app.api.deps import CurrentUser, OptionalUser, SessionDep
from app.core import rate_limit
from app.core.envelope import CollectionEnvelope, Envelope
from app.domains.catalog.schemas import CamelModel, ExperienceSummary
from app.domains.catalog.serializers import to_summary
from app.domains.explorer.collections import (
    MAX_DESCRIPTION,
    MAX_NOTE,
    MAX_TITLE,
    CollectionService,
    CollectionSummary,
    assert_can_publish,
)
from app.domains.explorer.models import VISIBILITY_PRIVATE, Collection

router = APIRouter(tags=["collections"])


class CollectionOut(CamelModel):
    id: uuid.UUID
    slug: str
    title: str
    description: str | None = None
    city_slug: str | None = None
    visibility: str
    source: str
    item_count: int
    preview_image_urls: list[str] = []
    is_mine: bool = False
    # Only meaningful for public ones, and only shown to the owner - a reader
    # does not need to know a page is awaiting review, they need it to be
    # withheld, which the listing query already does.
    moderation_status: str | None = None


class CollectionDetailOut(CollectionOut):
    experiences: list[ExperienceSummary] = []
    # Keyed by experience id: the curator's reason for including each one. The
    # single most useful thing a shared collection carries.
    notes: dict[uuid.UUID, str] = {}


class CreateCollectionRequest(CamelModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION)
    city_slug: str | None = None
    visibility: str = VISIBILITY_PRIVATE


class UpdateCollectionRequest(CamelModel):
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION)
    visibility: str | None = None


class AddItemRequest(CamelModel):
    experience_id: uuid.UUID
    note: str | None = Field(default=None, max_length=MAX_NOTE)


class NoteRequest(CamelModel):
    note: str | None = Field(default=None, max_length=MAX_NOTE)


class ReorderRequest(CamelModel):
    # The whole list, not a move. Two tabs each nudging one item converge this
    # way and diverge with pairwise swaps.
    experience_ids: list[uuid.UUID]


def _summary_out(summary: CollectionSummary, *, viewer_id: uuid.UUID | None) -> CollectionOut:
    collection = summary.collection
    mine = viewer_id is not None and collection.user_id == viewer_id
    return CollectionOut(
        id=collection.id,
        slug=collection.slug,
        title=collection.title,
        description=collection.description,
        city_slug=collection.city_slug,
        visibility=collection.visibility,
        source=collection.source,
        item_count=summary.item_count,
        preview_image_urls=summary.preview_image_urls,
        is_mine=mine,
        moderation_status=collection.moderation_status if mine else None,
    )


async def _detail_out(
    service: CollectionService, collection: Collection, *, viewer_id: uuid.UUID | None
) -> CollectionDetailOut:
    experiences = await service.experiences_in(collection)
    mine = viewer_id is not None and collection.user_id == viewer_id
    return CollectionDetailOut(
        id=collection.id,
        slug=collection.slug,
        title=collection.title,
        description=collection.description,
        city_slug=collection.city_slug,
        visibility=collection.visibility,
        source=collection.source,
        item_count=len(collection.items),
        preview_image_urls=[],
        is_mine=mine,
        moderation_status=collection.moderation_status if mine else None,
        experiences=[to_summary(e) for e in experiences],
        notes={i.experience_id: i.note for i in collection.items if i.note},
    )


# ------------------------------------------------------------------- reading


@router.get(
    "/collections",
    response_model=CollectionEnvelope[CollectionOut],
    summary="Browse public collections",
)
async def public_collections(
    session: SessionDep,
    user: OptionalUser,
    city: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
) -> CollectionEnvelope[CollectionOut]:
    summaries = await CollectionService(session).public(city_slug=city, limit=limit)
    viewer_id = user.id if user else None
    return CollectionEnvelope(data=[_summary_out(s, viewer_id=viewer_id) for s in summaries])


@router.get(
    "/collections/mine",
    response_model=CollectionEnvelope[CollectionOut],
    summary="Your collections",
)
async def my_collections(
    session: SessionDep, user: CurrentUser
) -> CollectionEnvelope[CollectionOut]:
    summaries = await CollectionService(session).mine(user)
    return CollectionEnvelope(data=[_summary_out(s, viewer_id=user.id) for s in summaries])


@router.get(
    "/collections/{collection_id}",
    response_model=Envelope[CollectionDetailOut],
    summary="One collection",
    description=(
        "Works without an account, so a shared link works for the person it was "
        "shared with. A private collection answers 404 to anyone but its owner - "
        "403 would confirm it exists, which is the one thing private prevents."
    ),
)
async def get_collection(
    collection_id: uuid.UUID, session: SessionDep, user: OptionalUser
) -> Envelope[CollectionDetailOut]:
    service = CollectionService(session)
    collection = await service.get(collection_id, viewer=user)
    return Envelope(
        data=await _detail_out(service, collection, viewer_id=user.id if user else None)
    )


@router.get(
    "/collections/by-slug/{slug}",
    response_model=Envelope[CollectionDetailOut],
    summary="One collection, by its shareable slug",
)
async def get_collection_by_slug(
    slug: str, session: SessionDep, user: OptionalUser
) -> Envelope[CollectionDetailOut]:
    service = CollectionService(session)
    collection = await service.by_slug(slug, viewer=user)
    return Envelope(
        data=await _detail_out(service, collection, viewer_id=user.id if user else None)
    )


# ------------------------------------------------------------------- writing


@router.post(
    "/collections",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[CollectionDetailOut],
    summary="Start a collection",
)
async def create_collection(
    payload: CreateCollectionRequest,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
) -> Envelope[CollectionDetailOut]:
    await rate_limit.check(rate_limit.identify(request, str(user.id)), rate_limit.DRAFT_LIMIT)
    service = CollectionService(session)
    collection = await service.create(
        user,
        title=payload.title,
        description=payload.description,
        city_slug=payload.city_slug,
        visibility=payload.visibility,
    )
    view = await _detail_out(service, collection, viewer_id=user.id)
    await session.commit()
    return Envelope(data=view)


@router.patch(
    "/collections/{collection_id}",
    response_model=Envelope[CollectionDetailOut],
    summary="Rename, describe, or change who can see it",
)
async def update_collection(
    collection_id: uuid.UUID,
    payload: UpdateCollectionRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[CollectionDetailOut]:
    service = CollectionService(session)
    collection = await service.update(
        user,
        collection_id,
        title=payload.title,
        description=payload.description,
        visibility=payload.visibility,
    )

    # Screened on the way to public, and only then. Going public is asking the
    # platform to show this to strangers; private and unlisted are not.
    if payload.visibility == "public":
        assert_can_publish(collection)
        from app.domains.trust.service import TrustService

        await TrustService(session).screen_collection(collection)

    view = await _detail_out(service, collection, viewer_id=user.id)
    await session.commit()
    return Envelope(data=view)


@router.delete(
    "/collections/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a collection",
)
async def delete_collection(
    collection_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> None:
    await CollectionService(session).delete(user, collection_id)
    await session.commit()


# --------------------------------------------------------------------- items


@router.post(
    "/collections/{collection_id}/items",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[CollectionDetailOut],
    summary="Add an experience",
)
async def add_item(
    collection_id: uuid.UUID,
    payload: AddItemRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[CollectionDetailOut]:
    service = CollectionService(session)
    await service.add(user, collection_id, payload.experience_id, note=payload.note)
    collection = await service.get(collection_id, viewer=user)
    view = await _detail_out(service, collection, viewer_id=user.id)
    await session.commit()
    return Envelope(data=view)


@router.delete(
    "/collections/{collection_id}/items/{experience_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an experience",
)
async def remove_item(
    collection_id: uuid.UUID,
    experience_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
) -> None:
    await CollectionService(session).remove(user, collection_id, experience_id)
    await session.commit()


@router.put(
    "/collections/{collection_id}/items/{experience_id}/note",
    response_model=Envelope[CollectionDetailOut],
    summary="Say why this one is in the list",
)
async def annotate_item(
    collection_id: uuid.UUID,
    experience_id: uuid.UUID,
    payload: NoteRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[CollectionDetailOut]:
    service = CollectionService(session)
    await service.annotate(user, collection_id, experience_id, payload.note)
    collection = await service.get(collection_id, viewer=user)
    view = await _detail_out(service, collection, viewer_id=user.id)
    await session.commit()
    return Envelope(data=view)


@router.put(
    "/collections/{collection_id}/order",
    response_model=Envelope[CollectionDetailOut],
    summary="Set the display order",
    description=(
        "Display order only. A collection makes no claim about sequence, time or "
        "feasibility - that is what an itinerary is for."
    ),
)
async def reorder_items(
    collection_id: uuid.UUID,
    payload: ReorderRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[CollectionDetailOut]:
    service = CollectionService(session)
    collection = await service.reorder(user, collection_id, payload.experience_ids)
    view = await _detail_out(service, collection, viewer_id=user.id)
    await session.commit()
    return Envelope(data=view)

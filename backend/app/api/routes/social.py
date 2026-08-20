"""Reposting a listing.

Mounted under the experience it belongs to, because that is what it is about and
it keeps the id in one place rather than in a body somebody can forget to
validate.

Likes and comments were here and were removed: reviews already carry a rating
and a written opinion, one per person, and a like plus a comment is a weaker
copy of both. See `explorer/social.py`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request

from app.api.deps import CurrentUser, SessionDep
from app.core import rate_limit
from app.core.envelope import Envelope
from app.domains.catalog.schemas import CamelModel
from app.domains.explorer.social import SocialService

router = APIRouter(tags=["social"])


class ReactionOut(CamelModel):
    """The state of the toggle after it was pressed.

    Both the new state and the new count, so the button can settle without a
    refetch - and so two rapid taps cannot leave it drawn one way and counted
    the other.
    """

    active: bool
    count: int


class RepostRequest(CamelModel):
    note: str | None = None


@router.post(
    "/experiences/{experience_id}/repost",
    response_model=Envelope[ReactionOut],
    summary="Repost a listing, or undo it",
    description=(
        "A toggle: pressing it again removes the repost. The optional note is a "
        "caption. Anything longer or more considered belongs in a review, which "
        "carries a rating and counts towards the listing's average."
    ),
)
async def toggle_repost(
    experience_id: uuid.UUID,
    payload: RepostRequest,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
) -> Envelope[ReactionOut]:
    await rate_limit.check(
        rate_limit.identify(request, str(user.id)), rate_limit.INTERACTION_LIMIT
    )
    reposted, count = await SocialService(session).toggle_repost(
        user, experience_id, note=payload.note
    )
    await session.commit()
    return Envelope(data=ReactionOut(active=reposted, count=count))

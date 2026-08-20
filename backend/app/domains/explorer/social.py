"""Reposts.

Sharing somebody else's listing with your own audience.

**Likes and comments used to live here and were removed.** Mado already has
reviews - a rating and a written opinion, one per person per listing, weighted
by verified attendance and feeding the listing's average. A like is a weaker
version of the rating and a comment is a weaker version of the opinion. Carrying
both meant two places to say the same thing, two things to moderate, and a
reader having to look in two places to learn what people thought.

A repost is not a weaker review. It says "other people should see this", which a
review does not say and cannot.

The count on `catalog.experiences.repost_count` is written here and nowhere
else, and is **recomputed from the rows** rather than incremented: an increment
drifts the first time a request fails between the insert and the update, and a
drifted count is invisible until somebody counts by hand.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.domains.catalog.models import Experience
from app.domains.explorer.models import Repost
from app.domains.identity.models import User

logger = get_logger("mado.explorer.social")

# A note on a repost is a caption, not an essay. Anything longer is a review.
MAX_REPOST_NOTE = 500


class SocialService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def toggle_repost(
        self, user: User, experience_id: uuid.UUID, *, note: str | None = None
    ) -> tuple[bool, int]:
        """Repost or undo it. Returns `(reposted_now, count)`.

        A toggle rather than separate add/remove endpoints, because the client
        has one button and the two would disagree the first time a tap was
        double-sent: "repost" on an already-reposted row is either an error or a
        no-op, and neither answer tells the button what to draw.
        """
        await self._assert_visible(experience_id)

        existing = await self.session.scalar(
            select(Repost).where(
                Repost.user_id == user.id, Repost.experience_id == experience_id
            )
        )
        if existing is not None:
            await self.session.delete(existing)
            reposted = False
        else:
            cleaned = (note or "").strip()[:MAX_REPOST_NOTE] or None
            self.session.add(
                Repost(user_id=user.id, experience_id=experience_id, note=cleaned)
            )
            reposted = True

        # Flush before recounting: the count is computed from the rows, and the
        # insert or delete above is not visible to a query until it lands.
        await self.session.flush()
        count = await self._recount(experience_id)
        logger.info(
            "repost_toggled",
            experience_id=str(experience_id),
            user_id=str(user.id),
            active=reposted,
        )
        return reposted, count

    async def reposted_experience_ids(
        self, user_id: uuid.UUID | None, experience_ids: list[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these the explorer has reposted, in one query.

        Answered in bulk for the same reason the count is denormalised: a feed
        asks about every card at once, and asking per card is the N+1 again.
        """
        if user_id is None or not experience_ids:
            return set()
        rows = await self.session.execute(
            select(Repost.experience_id).where(
                Repost.user_id == user_id, Repost.experience_id.in_(experience_ids)
            )
        )
        return set(rows.scalars().all())

    async def all_reposted_ids(self, user_id: uuid.UUID | None) -> set[str]:
        """Everything this explorer has reposted.

        The same shape as `ExplorerService.saved_experience_ids`, and used where
        the ids being rendered are not known in advance - the concierge decides
        what to show only after its tools have run, so there is nothing to ask
        about until it is too late to ask.

        Ids are strings because that is what `RankingContext` compares against.
        """
        if user_id is None:
            return set()
        rows = await self.session.execute(
            select(Repost.experience_id).where(Repost.user_id == user_id)
        )
        return {str(row) for row in rows.scalars().all()}

    async def reposts_by(self, user_id: uuid.UUID, *, limit: int = 50) -> list[Repost]:
        """What this account has reposted, newest first."""
        rows = await self.session.execute(
            select(Repost)
            .where(Repost.user_id == user_id)
            .order_by(Repost.created_at.desc())
            .limit(limit)
        )
        return list(rows.scalars().all())

    # ---------------------------------------------------------------- helpers

    async def _assert_visible(self, experience_id: uuid.UUID) -> Experience:
        """Refuse to attach anything to a listing nobody can see.

        Without this, a draft's id is enough to repost it - and the author would
        find their unpublished work circulating.
        """
        experience = await self.session.get(Experience, experience_id)
        if experience is None or not experience.is_discoverable:
            raise NotFoundError("That listing is not available.", code="EXPERIENCE_NOT_FOUND")
        return experience

    async def _recount(self, experience_id: uuid.UUID) -> int:
        """Recompute the denormalised count from the rows themselves.

        Recomputed rather than incremented. An increment is one lost request away
        from being wrong for ever, and nothing would notice - whereas this is
        self-correcting on the next interaction.
        """
        experience = await self.session.get(Experience, experience_id)
        if experience is None:
            return 0

        reposts = await self.session.scalar(
            select(func.count())
            .select_from(Repost)
            .where(Repost.experience_id == experience_id)
        )
        experience.repost_count = reposts or 0
        await self.session.flush()
        return experience.repost_count

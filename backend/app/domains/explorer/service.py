"""Explorer service - saved items, preferences and interaction signals."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, NotFoundError
from app.domains.explorer.models import InteractionEvent, SavedItem
from app.domains.identity.models import User, UserProfile

SAVEABLE_ENTITY_TYPES = {"experience", "event", "venue"}


class ExplorerService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------ saved items

    async def save(
        self,
        user: User,
        *,
        entity_type: str,
        entity_id: uuid.UUID,
        note: str | None = None,
    ) -> SavedItem:
        if entity_type not in SAVEABLE_ENTITY_TYPES:
            raise BadRequestError(
                f"Cannot save entities of type '{entity_type}'.",
                code="UNSUPPORTED_ENTITY_TYPE",
                details={"supported": sorted(SAVEABLE_ENTITY_TYPES)},
            )

        existing = await self._find_saved(user.id, entity_type, entity_id)
        if existing is not None:
            # Saving twice is the same intent as saving once; treat it as success
            # so a double-tap does not surface an error.
            if note is not None:
                existing.note = note
            return existing

        item = SavedItem(user_id=user.id, entity_type=entity_type, entity_id=entity_id, note=note)
        self.session.add(item)
        await self.session.flush()
        await self.record_interaction(
            user_id=user.id, action="save", entity_type=entity_type, entity_id=entity_id, weight=3
        )
        return item

    async def unsave(self, user: User, *, entity_type: str, entity_id: uuid.UUID) -> None:
        existing = await self._find_saved(user.id, entity_type, entity_id)
        if existing is None:
            raise NotFoundError("That item is not saved.", code="SAVED_ITEM_NOT_FOUND")
        await self.session.execute(delete(SavedItem).where(SavedItem.id == existing.id))
        await self.record_interaction(
            user_id=user.id, action="unsave", entity_type=entity_type, entity_id=entity_id
        )

    async def _find_saved(
        self, user_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
    ) -> SavedItem | None:
        result = await self.session.execute(
            select(SavedItem).where(
                SavedItem.user_id == user_id,
                SavedItem.entity_type == entity_type,
                SavedItem.entity_id == entity_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_saved(
        self, user_id: uuid.UUID, *, entity_type: str | None = None
    ) -> list[SavedItem]:
        stmt = select(SavedItem).where(SavedItem.user_id == user_id)
        if entity_type:
            stmt = stmt.where(SavedItem.entity_type == entity_type)
        result = await self.session.execute(stmt.order_by(SavedItem.created_at.desc()))
        return list(result.scalars().all())

    async def saved_experience_ids(self, user_id: uuid.UUID | None) -> set[str]:
        """Ids used to flag cards as already-saved across every discovery surface."""
        if user_id is None:
            return set()
        result = await self.session.execute(
            select(SavedItem.entity_id).where(
                SavedItem.user_id == user_id, SavedItem.entity_type == "experience"
            )
        )
        return {str(row) for row in result.scalars().all()}

    # ------------------------------------------------------------ preferences

    async def update_preferences(self, profile: UserProfile, changes: dict) -> UserProfile:
        """Merge preference changes into the Living Explorer Profile.

        Merged rather than replaced so a partial update from one onboarding step
        cannot wipe preferences captured by another (spec 10.01.02).
        """
        preferences = dict(profile.preferences or {})
        for key, value in changes.items():
            if value is None:
                continue
            preferences[key] = value
        preferences["updatedAt"] = datetime.now(UTC).isoformat()
        profile.preferences = preferences
        return profile

    async def update_privacy(self, profile: UserProfile, changes: dict) -> UserProfile:
        privacy = dict(profile.privacy or {})
        for key, value in changes.items():
            if value is None:
                continue
            privacy[key] = value
        profile.privacy = privacy
        return profile

    # ------------------------------------------------------------ interactions

    async def record_interaction(
        self,
        *,
        user_id: uuid.UUID | None = None,
        anonymous_id: str | None = None,
        action: str,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        weight: int = 1,
        context: dict | None = None,
    ) -> None:
        """Append a behavioural signal.

        Fire-and-forget by design: personalization must never be able to fail a
        user-facing request (spec 10.01.02 "Progressive Learning").
        """
        if user_id is None and anonymous_id is None:
            return
        self.session.add(
            InteractionEvent(
                user_id=user_id,
                anonymous_id=anonymous_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                weight=weight,
                context=context or {},
                occurred_at=datetime.now(UTC),
            )
        )

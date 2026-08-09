"""Publishing service.

Anyone with an account can post. The first time an explorer publishes, a personal
publisher is created for them from their profile - no organization form, no
approval step, no waiting.

Three rules hold throughout:

* **Ownership is checked on every mutation.** Being signed in is not authority
  over someone else's post.
* **Platform-owned fields are never accepted from input.** Popularity, quality,
  trend, ratings and moderation state are computed by the platform (spec 54.03
  s15); a publisher who could set their own ranking score would have found the
  cheapest possible growth hack.
* **Publishing is reversible.** Unpublish and archive exist so an author is never
  stuck with something they regret.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.domains.catalog.models import (
    MODERATION_APPROVED,
    MODERATION_REJECTED,
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_PUBLISHED,
    TYPE_EVENT,
    Category,
    City,
    EventInstance,
    Experience,
    Media,
    Tag,
    Venue,
)
from app.domains.identity.models import User
from app.domains.publisher.models import (
    TRUST_LEVEL_COMMUNITY,
    TYPE_INDIVIDUAL,
    Publisher,
)

logger = get_logger("mado.publishing")

MAX_MEDIA_PER_EXPERIENCE = 10
MAX_EVENTS_PER_EXPERIENCE = 60


def slugify(value: str, *, max_length: int = 60) -> str:
    """URL-safe slug from arbitrary user text.

    Amharic and other non-Latin titles normalise away to nothing, so the caller
    must be prepared for an empty result and fall back to an opaque id rather than
    producing a slug like "--".
    """
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return slug[:max_length].strip("-")


class PublishingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------- publishers

    async def personal_publisher(self, user: User) -> Publisher:
        """Return the explorer's personal publisher, creating it on first use.

        This is what makes publishing feel like posting: the identity is derived
        from the profile the explorer already has.
        """
        result = await self.session.execute(
            select(Publisher).where(
                Publisher.owner_user_id == user.id,
                Publisher.type == TYPE_INDIVIDUAL,
                Publisher.deleted_at.is_(None),
            )
        )
        publisher = result.scalar_one_or_none()
        if publisher is not None:
            return publisher

        display_name = user.profile.display_name if user.profile else "Explorer"
        publisher = Publisher(
            name=display_name,
            slug=await self._unique_publisher_slug(display_name, user.id),
            type=TYPE_INDIVIDUAL,
            verification_status="unverified",
            trust_level=TRUST_LEVEL_COMMUNITY,
            owner_user_id=user.id,
            logo_url=user.profile.avatar_url if user.profile else None,
        )
        self.session.add(publisher)
        await self.session.flush()
        logger.info("personal_publisher_created", user_id=str(user.id))
        return publisher

    async def _unique_publisher_slug(self, name: str, user_id: uuid.UUID) -> str:
        base = slugify(name) or f"explorer-{user_id.hex[:8]}"
        candidate = base
        for suffix in range(0, 50):
            if suffix:
                candidate = f"{base}-{suffix}"
            exists = await self.session.scalar(
                select(func.count()).select_from(Publisher).where(Publisher.slug == candidate)
            )
            if not exists:
                return candidate
        # Collision-proof fallback rather than looping forever on a popular name.
        return f"{base}-{uuid.uuid4().hex[:6]}"

    async def assert_can_publish_as(self, user: User, publisher_id: uuid.UUID) -> Publisher:
        """Resolve a publisher the explorer is entitled to post as."""
        publisher = await self.session.get(Publisher, publisher_id)
        if publisher is None or publisher.deleted_at is not None:
            raise NotFoundError("Publisher not found.", code="PUBLISHER_NOT_FOUND")
        if publisher.owner_user_id != user.id:
            raise PermissionDeniedError(
                "You cannot post as this publisher.", code="NOT_PUBLISHER_OWNER"
            )
        return publisher

    # ------------------------------------------------------------ experiences

    async def _load_owned(self, user: User, experience_id: uuid.UUID) -> Experience:
        result = await self.session.execute(
            select(Experience)
            .where(Experience.id == experience_id, Experience.deleted_at.is_(None))
            .options(
                selectinload(Experience.publisher),
                selectinload(Experience.media),
                selectinload(Experience.events),
                selectinload(Experience.tags),
                selectinload(Experience.venue).selectinload(Venue.neighborhood),
                selectinload(Experience.category),
                selectinload(Experience.city),
            )
        )
        experience = result.scalar_one_or_none()
        if experience is None:
            raise NotFoundError("Experience not found.", code="EXPERIENCE_NOT_FOUND")

        publisher = experience.publisher
        if publisher is None or publisher.owner_user_id != user.id:
            # Deliberately 404, not 403: confirming that someone else's draft
            # exists is itself a disclosure.
            raise NotFoundError("Experience not found.", code="EXPERIENCE_NOT_FOUND")
        return experience

    async def _unique_experience_slug(self, title: str) -> str:
        base = slugify(title) or f"experience-{uuid.uuid4().hex[:8]}"
        candidate = base
        for suffix in range(0, 50):
            if suffix:
                candidate = f"{base}-{suffix}"
            exists = await self.session.scalar(
                select(func.count()).select_from(Experience).where(Experience.slug == candidate)
            )
            if not exists:
                return candidate
        return f"{base}-{uuid.uuid4().hex[:6]}"

    async def _resolve_city(self, city_slug: str) -> City:
        result = await self.session.execute(select(City).where(City.slug == city_slug))
        city = result.scalar_one_or_none()
        if city is None:
            raise BadRequestError(
                f"Unknown city '{city_slug}'.",
                code="CITY_NOT_FOUND",
                details={"citySlug": city_slug},
            )
        return city

    async def _resolve_category(self, slug: str | None) -> Category | None:
        if not slug:
            return None
        result = await self.session.execute(select(Category).where(Category.slug == slug))
        category = result.scalar_one_or_none()
        if category is None:
            raise BadRequestError(f"Unknown category '{slug}'.", code="CATEGORY_NOT_FOUND")
        return category

    async def _resolve_tags(self, slugs: list[str] | None) -> list[Tag]:
        """Resolve tag slugs, silently ignoring unknown ones.

        Tags are a soft classification. Rejecting a whole post because one tag was
        misspelled would be a poor trade for the author.
        """
        if not slugs:
            return []
        result = await self.session.execute(select(Tag).where(Tag.slug.in_(slugs)))
        return list(result.scalars().all())

    async def create_experience(
        self,
        user: User,
        *,
        title: str,
        description: str,
        city_slug: str,
        experience_type: str,
        summary: str | None = None,
        category_slug: str | None = None,
        venue_id: uuid.UUID | None = None,
        tags: list[str] | None = None,
        price_type: str = "free",
        price_amount: float | None = None,
        price_max: float | None = None,
        currency: str | None = None,
        duration_minutes: int | None = None,
        is_indoor: bool | None = None,
        accessibility: dict | None = None,
        publisher_id: uuid.UUID | None = None,
    ) -> Experience:
        """Create a draft. Nothing is visible until the author publishes it."""
        publisher = (
            await self.assert_can_publish_as(user, publisher_id)
            if publisher_id
            else await self.personal_publisher(user)
        )
        city = await self._resolve_city(city_slug)
        category = await self._resolve_category(category_slug)

        venue = None
        if venue_id is not None:
            venue = await self.session.get(Venue, venue_id)
            if venue is None or venue.deleted_at is not None:
                raise BadRequestError("Unknown venue.", code="VENUE_NOT_FOUND")

        if price_type != "free" and price_amount is None:
            raise ValidationError(
                "A price is required unless the experience is free.",
                code="PRICE_REQUIRED",
            )

        experience = Experience(
            publisher_id=publisher.id,
            city_id=city.id,
            venue_id=venue.id if venue else None,
            category_id=category.id if category else None,
            title=title.strip(),
            slug=await self._unique_experience_slug(title),
            summary=(summary or "").strip() or None,
            description=description.strip(),
            type=experience_type,
            status=STATUS_DRAFT,
            price_type=price_type,
            price_amount=price_amount,
            price_max=price_max,
            currency=(currency or city.currency).upper(),
            duration_minutes=duration_minutes,
            is_indoor=is_indoor,
            accessibility=accessibility or {},
            attributes={},
            tags=await self._resolve_tags(tags),
            media=[],
            events=[],
        )
        self.session.add(experience)
        await self.session.flush()
        logger.info(
            "experience_created", experience_id=str(experience.id), publisher_id=str(publisher.id)
        )
        # Re-load with relations eagerly attached. A freshly constructed instance
        # has none of them loaded, and serializing it would trigger a lazy load -
        # which raises MissingGreenlet under an async session.
        return await self._load_owned(user, experience.id)

    async def update_experience(
        self, user: User, experience_id: uuid.UUID, changes: dict
    ) -> Experience:
        experience = await self._load_owned(user, experience_id)
        if experience.status == STATUS_ARCHIVED:
            raise ConflictError(
                "Restore this experience before editing it.", code="EXPERIENCE_ARCHIVED"
            )

        simple_fields = {
            "title",
            "summary",
            "description",
            "type",
            "price_type",
            "price_amount",
            "price_max",
            "currency",
            "duration_minutes",
            "is_indoor",
            "accessibility",
        }
        for field, value in changes.items():
            if field in simple_fields and value is not None:
                setattr(experience, field, value)

        if changes.get("city_slug"):
            experience.city_id = (await self._resolve_city(changes["city_slug"])).id
        if "category_slug" in changes:
            category = await self._resolve_category(changes["category_slug"])
            experience.category_id = category.id if category else None
        if "tags" in changes and changes["tags"] is not None:
            experience.tags = await self._resolve_tags(changes["tags"])
        if "venue_id" in changes:
            venue_id = changes["venue_id"]
            if venue_id is None:
                experience.venue_id = None
            else:
                venue = await self.session.get(Venue, venue_id)
                if venue is None or venue.deleted_at is not None:
                    raise BadRequestError("Unknown venue.", code="VENUE_NOT_FOUND")
                experience.venue_id = venue.id

        if experience.price_type != "free" and experience.price_amount is None:
            raise ValidationError(
                "A price is required unless the experience is free.", code="PRICE_REQUIRED"
            )

        await self.session.flush()
        return experience

    async def publish(self, user: User, experience_id: uuid.UUID) -> Experience:
        """Make a draft discoverable, after checking it is actually usable."""
        experience = await self._load_owned(user, experience_id)

        if experience.moderation_status == MODERATION_REJECTED:
            raise PermissionDeniedError(
                "This experience was removed by moderation and cannot be republished.",
                code="MODERATION_REJECTED",
            )

        problems = self.readiness_problems(experience)
        if problems:
            raise ValidationError(
                "This experience is not ready to publish yet.",
                code="EXPERIENCE_INCOMPLETE",
                details={"problems": problems},
            )

        experience.status = STATUS_PUBLISHED
        if experience.published_at is None:
            experience.published_at = datetime.now(UTC)
        await self.session.flush()
        await self._announce("experience.published", user, experience)
        logger.info("experience_published", experience_id=str(experience.id))
        return experience

    async def unpublish(self, user: User, experience_id: uuid.UUID) -> Experience:
        experience = await self._load_owned(user, experience_id)
        experience.status = STATUS_DRAFT
        await self.session.flush()
        await self._announce("experience.unpublished", user, experience)
        return experience

    async def _announce(self, event_type: str, user: User, experience: Experience) -> None:
        """Queue a webhook for anyone subscribed (spec DEV-003).

        In this transaction rather than after it, so an announcement cannot
        survive a rollback of the thing it announces. Nothing is sent from here;
        the scheduler drains the queue.

        The payload is identifiers and the title - enough for a receiver to know
        what changed and fetch the rest. Sending the whole record would put a
        copy of the catalogue in somebody's logs and go stale the moment it
        left.
        """
        from app.domains.developer.webhooks import emit

        await emit(
            self.session,
            event_type=event_type,
            owner_user_id=user.id,
            data={
                "experienceId": str(experience.id),
                "slug": experience.slug,
                "title": experience.title,
                "status": experience.status,
            },
        )

    async def archive(self, user: User, experience_id: uuid.UUID) -> Experience:
        experience = await self._load_owned(user, experience_id)
        experience.status = STATUS_ARCHIVED
        await self.session.flush()
        return experience

    async def restore(self, user: User, experience_id: uuid.UUID) -> Experience:
        experience = await self._load_owned(user, experience_id)
        if experience.status != STATUS_ARCHIVED:
            raise ConflictError("That experience is not archived.", code="NOT_ARCHIVED")
        # Back to draft, never straight to published: the author decides when it
        # goes live again.
        experience.status = STATUS_DRAFT
        await self.session.flush()
        return experience

    @staticmethod
    def readiness_problems(experience: Experience) -> list[str]:
        """Human-readable reasons an experience cannot go live yet.

        Phrased for the author rather than as validation codes, because this list
        is shown directly in the composer.
        """
        problems: list[str] = []
        if len(experience.title.strip()) < 4:
            problems.append("Give it a title of at least 4 characters.")
        if len(experience.description.strip()) < 40:
            problems.append("Add a description of at least 40 characters.")
        if experience.category_id is None:
            problems.append("Choose a category so people can find it.")
        if experience.venue_id is None:
            problems.append("Add a location.")
        if experience.type == TYPE_EVENT and not [
            event for event in (experience.events or []) if event.status != "cancelled"
        ]:
            problems.append("Add at least one date and time.")
        if experience.price_type != "free" and experience.price_amount is None:
            problems.append("Set a price, or mark it as free.")
        return problems

    # ----------------------------------------------------------------- media

    async def add_media(
        self, user: User, experience_id: uuid.UUID, *, url: str, alt_text: str | None = None
    ) -> Media:
        experience = await self._load_owned(user, experience_id)
        if len(experience.media or []) >= MAX_MEDIA_PER_EXPERIENCE:
            raise ConflictError(
                f"An experience can have at most {MAX_MEDIA_PER_EXPERIENCE} images.",
                code="MEDIA_LIMIT_REACHED",
            )
        # Two legitimate shapes: an absolute http(s) URL, or a path under /media/
        # produced by our own upload endpoint. The second is deliberately narrow -
        # accepting arbitrary relative paths would let a caller point a listing at
        # any route on this host, and "/media/" is the only one we serve files from.
        if not url.startswith(("https://", "http://", "/media/")):
            raise ValidationError(
                "Image URL must be http(s), or an uploaded image.",
                code="INVALID_MEDIA_URL",
            )
        if url.startswith("/media/") and ".." in url:
            raise ValidationError("Invalid image path.", code="INVALID_MEDIA_URL")

        media = Media(
            experience_id=experience.id,
            type="image",
            url=url,
            alt_text=alt_text,
            sort_order=len(experience.media or []),
        )
        self.session.add(media)
        await self.session.flush()
        return media

    async def remove_media(self, user: User, experience_id: uuid.UUID, media_id: uuid.UUID) -> None:
        experience = await self._load_owned(user, experience_id)
        media = next((m for m in (experience.media or []) if m.id == media_id), None)
        if media is None:
            raise NotFoundError("Image not found.", code="MEDIA_NOT_FOUND")
        await self.session.delete(media)
        await self.session.flush()

    # ---------------------------------------------------------------- events

    async def add_event(
        self,
        user: User,
        experience_id: uuid.UUID,
        *,
        start_time: datetime,
        end_time: datetime | None = None,
        capacity: int | None = None,
    ) -> EventInstance:
        experience = await self._load_owned(user, experience_id)

        if len(experience.events or []) >= MAX_EVENTS_PER_EXPERIENCE:
            raise ConflictError(
                f"An experience can have at most {MAX_EVENTS_PER_EXPERIENCE} dates.",
                code="EVENT_LIMIT_REACHED",
            )
        if end_time is not None and end_time <= start_time:
            raise ValidationError(
                "The end time must be after the start time.", code="INVALID_EVENT_WINDOW"
            )
        if start_time < datetime.now(UTC):
            raise ValidationError("That start time is in the past.", code="EVENT_IN_PAST")

        event = EventInstance(
            experience_id=experience.id,
            start_time=start_time,
            end_time=end_time,
            status="scheduled",
            capacity=capacity,
            remaining=capacity,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def cancel_event(
        self,
        user: User,
        experience_id: uuid.UUID,
        event_id: uuid.UUID,
        *,
        reason: str | None = None,
    ) -> EventInstance:
        experience = await self._load_owned(user, experience_id)
        event = next((e for e in (experience.events or []) if e.id == event_id), None)
        if event is None:
            raise NotFoundError("Date not found.", code="EVENT_NOT_FOUND")
        # Cancelled rather than deleted: people may have planned around it, and
        # spec 57.03 s18 requires cancellation to be visible rather than silent.
        event.status = "cancelled"
        event.cancellation_reason = reason
        await self.session.flush()
        return event

    async def delete_event(self, user: User, experience_id: uuid.UUID, event_id: uuid.UUID) -> None:
        experience = await self._load_owned(user, experience_id)
        event = next((e for e in (experience.events or []) if e.id == event_id), None)
        if event is None:
            raise NotFoundError("Date not found.", code="EVENT_NOT_FOUND")
        if experience.status == STATUS_PUBLISHED:
            raise ConflictError(
                "Cancel this date instead - it is live and people may be relying on it.",
                code="CANCEL_INSTEAD_OF_DELETE",
            )
        await self.session.delete(event)
        await self.session.flush()

    # ------------------------------------------------------------- listing

    async def list_own_experiences(
        self, user: User, *, status: str | None = None
    ) -> list[Experience]:
        """Every experience across all publishers the explorer owns."""
        publisher_ids = (
            (
                await self.session.execute(
                    select(Publisher.id).where(
                        Publisher.owner_user_id == user.id, Publisher.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )

        if not publisher_ids:
            return []

        stmt = (
            select(Experience)
            .where(
                Experience.publisher_id.in_(list(publisher_ids)),
                Experience.deleted_at.is_(None),
            )
            .options(
                selectinload(Experience.publisher),
                selectinload(Experience.media),
                selectinload(Experience.events),
                selectinload(Experience.tags),
                selectinload(Experience.venue).selectinload(Venue.neighborhood),
                selectinload(Experience.category),
                selectinload(Experience.city),
            )
            .order_by(Experience.updated_at.desc())
        )
        if status:
            stmt = stmt.where(Experience.status == status)

        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_own_experience(self, user: User, experience_id: uuid.UUID) -> Experience:
        return await self._load_owned(user, experience_id)

    # ------------------------------------------------------------- venues

    async def create_venue(
        self,
        user: User,
        *,
        name: str,
        address: str,
        city_slug: str,
        latitude: float,
        longitude: float,
        neighborhood_id: uuid.UUID | None = None,
        accessibility: dict | None = None,
        publisher_id: uuid.UUID | None = None,
    ) -> Venue:
        """Create a location to attach experiences to.

        Explorers can add places that are not in the catalog yet - which is how a
        long tail of small venues gets in at all - but coordinates are validated so
        a typo cannot drop a café into the Gulf of Guinea.
        """
        publisher = (
            await self.assert_can_publish_as(user, publisher_id)
            if publisher_id
            else await self.personal_publisher(user)
        )
        city = await self._resolve_city(city_slug)

        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            raise ValidationError("Those coordinates are not valid.", code="INVALID_COORDINATES")

        venue = Venue(
            name=name.strip(),
            slug=await self._unique_venue_slug(name),
            publisher_id=publisher.id,
            city_id=city.id,
            neighborhood_id=neighborhood_id,
            address=address.strip(),
            latitude=latitude,
            longitude=longitude,
            facilities=[],
            accessibility=accessibility or {},
            opening_hours={},
            contact={},
        )
        self.session.add(venue)
        await self.session.flush()
        return venue

    async def _unique_venue_slug(self, name: str) -> str:
        base = slugify(name) or f"venue-{uuid.uuid4().hex[:8]}"
        candidate = base
        for suffix in range(0, 50):
            if suffix:
                candidate = f"{base}-{suffix}"
            exists = await self.session.scalar(
                select(func.count()).select_from(Venue).where(Venue.slug == candidate)
            )
            if not exists:
                return candidate
        return f"{base}-{uuid.uuid4().hex[:6]}"


__all__ = ["PublishingService", "slugify", "MODERATION_APPROVED"]

"""Catalog API schemas.

Wire field names are camelCase (spec 55.01 examples) while Python stays snake_case;
``alias_generator`` bridges the two so neither side compromises its conventions.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(word.capitalize() for word in tail)


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class CityOut(CamelModel):
    id: uuid.UUID
    name: str
    slug: str
    country: str
    country_code: str
    timezone: str
    currency: str
    languages: list[str] = Field(default_factory=list)
    latitude: float
    longitude: float
    is_live: bool


class NeighborhoodOut(CamelModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None = None
    latitude: float
    longitude: float


class CategoryOut(CamelModel):
    id: uuid.UUID
    name: str
    slug: str
    icon: str | None = None


class TagOut(CamelModel):
    id: uuid.UUID
    name: str
    slug: str


class MediaOut(CamelModel):
    id: uuid.UUID
    type: str
    url: str
    alt_text: str | None = None
    sort_order: int = 0


class PublisherSummary(CamelModel):
    id: uuid.UUID
    name: str
    slug: str
    logo_url: str | None = None
    verification_status: str
    trust_level: int
    # `individual` or `organization`. On a card this is what lets an explorer
    # tell "the cafe posted this" from "somebody who went there posted this",
    # which are different claims and were previously indistinguishable.
    type: str = "individual"
    # What kind of business, when it is one. Null for a person.
    business_type: str | None = None
    business_type_label: str | None = None

    @property
    def is_business(self) -> bool:
        return self.type == "organization"

    @property
    def is_verified(self) -> bool:
        return self.verification_status == "verified"


class VenueSummary(CamelModel):
    id: uuid.UUID
    name: str
    slug: str
    address: str
    latitude: float
    longitude: float
    neighborhood: NeighborhoodOut | None = None
    accessibility: dict = Field(default_factory=dict)
    # The building's own suitability claims, over the same vocabulary as an
    # experience's. Sent separately from the experience's union so an editor can
    # tell which record owns a claim - the composer must not offer to untick a
    # facility that belongs to the venue.
    facilities: list[str] = Field(default_factory=list)
    opening_hours: dict = Field(default_factory=dict)


class EventInstanceOut(CamelModel):
    id: uuid.UUID
    start_time: datetime
    end_time: datetime | None = None
    status: str
    capacity: int | None = None
    remaining: int | None = None
    cancellation_reason: str | None = None


class PriceOut(CamelModel):
    type: str
    amount: float | None = None
    max_amount: float | None = None
    currency: str


class ExperienceSummary(CamelModel):
    """Card-shaped payload.

    Spec 11.06 "Cards" requires every card to convey identity, category, time,
    location, publisher, trust and a primary action - so all of that is present
    here and the client never needs a second request to render a feed.
    """

    id: uuid.UUID
    title: str
    slug: str
    summary: str | None = None
    type: str
    category: CategoryOut | None = None
    tags: list[TagOut] = Field(default_factory=list)
    venue: VenueSummary | None = None
    city_slug: str | None = None
    price: PriceOut
    media: list[MediaOut] = Field(default_factory=list)
    publisher: PublisherSummary | None = None
    rating_average: float | None = None
    rating_count: int = 0
    next_event: EventInstanceOut | None = None
    duration_minutes: int | None = None
    is_indoor: bool | None = None
    # What this listing claims to be suitable for - the union of the experience's
    # own claims and its venue's, so a card can show the play area without the
    # client needing to know which record it was recorded on.
    suitability: list[str] = Field(default_factory=list)
    # Of what the explorer asked for, what this listing has not claimed. Never
    # "does not have": nobody said either way. Present so a card can hedge in the
    # same words the concierge uses instead of implying a promise by silence.
    unverified: list[str] = Field(default_factory=list)
    # Populated by the ranking layer; spec PRODUCT-00 principle 5 requires every
    # important recommendation to answer "why am I seeing this?".
    reason: str | None = None
    distance_km: float | None = None
    is_saved: bool = False
    # The count comes off the experience row, so a card costs no extra query;
    # `is_reposted` is filled in by the caller in one bulk lookup for the page.
    repost_count: int = 0
    is_reposted: bool = False


class ExperienceDetail(ExperienceSummary):
    description: str
    accessibility: dict = Field(default_factory=dict)
    attributes: dict = Field(default_factory=dict)
    upcoming_events: list[EventInstanceOut] = Field(default_factory=list)
    published_at: datetime | None = None
    updated_at: datetime | None = None


class EventOut(CamelModel):
    """An event instance presented with enough of its parent to stand alone."""

    id: uuid.UUID
    experience_id: uuid.UUID
    title: str
    summary: str | None = None
    start_time: datetime
    end_time: datetime | None = None
    status: str
    capacity: int | None = None
    remaining: int | None = None
    venue: VenueSummary | None = None
    category: CategoryOut | None = None
    price: PriceOut
    media: list[MediaOut] = Field(default_factory=list)
    publisher: PublisherSummary | None = None

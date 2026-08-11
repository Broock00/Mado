"""Publishing API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.domains.catalog.schemas import CamelModel, ExperienceDetail

VALID_EXPERIENCE_TYPES = {"place", "event", "activity"}
VALID_PRICE_TYPES = {"free", "fixed", "range"}


class CreateExperienceRequest(CamelModel):
    title: str = Field(min_length=4, max_length=240)
    description: str = Field(min_length=1, max_length=8000)
    city_slug: str
    type: str = "place"
    summary: str | None = Field(default=None, max_length=400)
    category_slug: str | None = None
    venue_id: uuid.UUID | None = None
    tags: list[str] | None = None
    price_type: str = "free"
    price_amount: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    duration_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 14)
    is_indoor: bool | None = None
    accessibility: dict | None = None
    # Where the publisher sells, if they sell somewhere else. Its presence
    # turns the details page's button outward instead of into a checkout, and
    # Mado then claims nothing about whether the transaction happened.
    external_ticket_url: str | None = Field(default=None, max_length=2000)
    publisher_id: uuid.UUID | None = None

    @field_validator("type")
    @classmethod
    def valid_type(cls, value: str) -> str:
        if value not in VALID_EXPERIENCE_TYPES:
            raise ValueError(f"type must be one of {sorted(VALID_EXPERIENCE_TYPES)}")
        return value

    @field_validator("price_type")
    @classmethod
    def valid_price_type(cls, value: str) -> str:
        if value not in VALID_PRICE_TYPES:
            raise ValueError(f"priceType must be one of {sorted(VALID_PRICE_TYPES)}")
        return value


class UpdateExperienceRequest(CamelModel):
    """Every field optional - the composer autosaves one section at a time."""

    title: str | None = Field(default=None, min_length=4, max_length=240)
    description: str | None = Field(default=None, min_length=1, max_length=8000)
    summary: str | None = Field(default=None, max_length=400)
    city_slug: str | None = None
    type: str | None = None
    category_slug: str | None = None
    venue_id: uuid.UUID | None = None
    tags: list[str] | None = None
    price_type: str | None = None
    price_amount: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    duration_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 14)
    is_indoor: bool | None = None
    accessibility: dict | None = None
    external_ticket_url: str | None = Field(default=None, max_length=2000)


class AddMediaRequest(CamelModel):
    url: str = Field(max_length=2000)
    alt_text: str | None = Field(default=None, max_length=400)


class AddEventRequest(CamelModel):
    start_time: datetime
    end_time: datetime | None = None
    capacity: int | None = Field(default=None, ge=1, le=1_000_000)


class CancelEventRequest(CamelModel):
    reason: str | None = Field(default=None, max_length=500)


class CreateVenueRequest(CamelModel):
    name: str = Field(min_length=2, max_length=200)
    address: str = Field(min_length=3, max_length=500)
    city_slug: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    neighborhood_id: uuid.UUID | None = None
    accessibility: dict | None = None


class VenueOut(CamelModel):
    id: uuid.UUID
    name: str
    slug: str
    address: str
    latitude: float
    longitude: float


class PublisherOut(CamelModel):
    id: uuid.UUID
    name: str
    slug: str
    type: str
    verification_status: str
    trust_level: int
    logo_url: str | None = None


class OwnExperienceOut(ExperienceDetail):
    """An author's view of their own post.

    Carries the editorial state the public payload deliberately omits: publishing
    status, moderation outcome, and what still blocks going live.
    """

    status: str
    moderation_status: str
    moderation_notes: str | None = None
    report_count: int = 0
    readiness_problems: list[str] = Field(default_factory=list)


class ReportRequest(CamelModel):
    reason: str
    detail: str | None = Field(default=None, max_length=1000)


class ReportOut(CamelModel):
    id: uuid.UUID
    experience_id: uuid.UUID
    reason: str
    status: str
    created_at: datetime


class ModerationDecisionRequest(CamelModel):
    approve: bool
    note: str | None = Field(default=None, max_length=1000)


class ModerationItemOut(CamelModel):
    id: uuid.UUID
    title: str
    summary: str | None = None
    publisher_name: str | None = None
    moderation_status: str
    moderation_notes: str | None = None
    report_count: int
    risk_score: float
    city_slug: str | None = None
    created_at: datetime

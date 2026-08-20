"""Publishing API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.domains.catalog import suitability as suitability_vocab
from app.domains.catalog.schemas import CamelModel, ExperienceDetail

VALID_EXPERIENCE_TYPES = {"place", "event", "activity"}
VALID_PRICE_TYPES = {"free", "fixed", "range"}


def _clean_suitability(value: list[str] | None) -> list[str] | None:
    """Drop anything outside the vocabulary rather than rejecting the request.

    A publisher who sends an unrecognised claim has their post saved without it,
    which is the right trade for a field several clients write: failing the whole
    save over one unknown slug would lose the description somebody just wrote.
    The claim is silently absent rather than silently stored, and absent means
    "nobody said" everywhere downstream - so nothing is asserted that was not
    understood.
    """
    if value is None:
        return None
    return suitability_vocab.normalise(value)


class CreateExperienceRequest(CamelModel):
    title: str = Field(min_length=4, max_length=240)
    description: str = Field(min_length=1, max_length=8000)
    # Optional: a post's city follows its venue, which was itself filed under the
    # city its coordinates are in. Required only for a post with no venue at all.
    city_slug: str | None = None
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
    # What this listing claims to be suitable for - a vegan menu, a play area,
    # step-free access. Slugs from `catalog/suitability.py`; see there for why an
    # omitted claim means "unknown" and never "no".
    suitability: list[str] | None = None
    # Where the publisher sells, if they sell somewhere else. Its presence
    # turns the details page's button outward instead of into a checkout, and
    # Mado then claims nothing about whether the transaction happened.
    external_ticket_url: str | None = Field(default=None, max_length=2000)
    publisher_id: uuid.UUID | None = None

    @field_validator("suitability")
    @classmethod
    def clean_suitability(cls, value: list[str] | None) -> list[str] | None:
        return _clean_suitability(value)

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
    suitability: list[str] | None = None
    external_ticket_url: str | None = Field(default=None, max_length=2000)

    @field_validator("suitability")
    @classmethod
    def clean_suitability(cls, value: list[str] | None) -> list[str] | None:
        return _clean_suitability(value)


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
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    # Optional, and normally omitted. The city is worked out from the coordinates
    # above, so a venue can be added anywhere on earth rather than only in a city
    # somebody had already typed into a table. Supply one only to override that.
    city_slug: str | None = None
    neighborhood_id: uuid.UUID | None = None
    accessibility: dict | None = None
    # The building's half of the suitability vocabulary: step-free access, a car
    # park, a play area. Kept on the venue because it stays true whoever is
    # performing tonight, while a fasting menu or a family matinee belongs to the
    # listing and goes in `CreateExperienceRequest.suitability`.
    facilities: list[str] | None = None
    # The identifier of the place these coordinates came from, when they came
    # from a search rather than a pin dropped on a map. Optional and never
    # required: a venue is located by its coordinates, and demanding an
    # identifier would make the map picker - the way most venues are added -
    # unable to create one.
    place_id: str | None = Field(default=None, max_length=512)

    @field_validator("facilities")
    @classmethod
    def clean_facilities(cls, value: list[str] | None) -> list[str] | None:
        return _clean_suitability(value)


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
    business_type: str | None = None
    cover_url: str | None = None


class CreateBusinessRequest(CamelModel):
    """Turn an existing account into the owner of a business profile.

    Deliberately short. The README lists fifteen fields a business might have;
    demanding them all before anything exists is how a publisher abandons the
    form halfway. Everything else is editable afterwards, and the location comes
    from creating a venue - which already has coordinates, an address and opening
    hours, and would be duplicated by putting them here.
    """

    name: str = Field(min_length=2, max_length=200)
    business_type: str | None = None
    description: str | None = Field(default=None, max_length=4000)
    website: str | None = Field(default=None, max_length=2000)
    contact: dict | None = None
    social: dict | None = None
    logo_url: str | None = Field(default=None, max_length=2000)
    cover_url: str | None = Field(default=None, max_length=2000)


class UpdateBusinessRequest(CamelModel):
    """Every field optional - the business dashboard saves a section at a time."""

    name: str | None = Field(default=None, min_length=2, max_length=200)
    business_type: str | None = None
    description: str | None = Field(default=None, max_length=4000)
    industry: str | None = Field(default=None, max_length=80)
    website: str | None = Field(default=None, max_length=2000)
    contact: dict | None = None
    social: dict | None = None
    logo_url: str | None = Field(default=None, max_length=2000)
    cover_url: str | None = Field(default=None, max_length=2000)


class InviteMemberRequest(CamelModel):
    """Invitations are addressed to an email, not to a user id.

    A business can invite whoever runs their front desk before that person has
    ever opened Mado; the membership waits for somebody with that address to
    accept it.
    """

    email: str = Field(min_length=3, max_length=320)
    role: str


class ChangeRoleRequest(CamelModel):
    role: str


class MemberOut(CamelModel):
    id: uuid.UUID
    role: str
    role_label: str
    status: str
    # Null while the invitation is unanswered - there is no account attached yet.
    user_id: uuid.UUID | None = None
    display_name: str | None = None
    invited_email: str | None = None
    invited_at: datetime | None = None
    expires_at: datetime | None = None
    # What this role actually permits, so the team screen explains a role from
    # the same source that enforces it rather than from a hard-coded caption.
    permissions: list[str] = Field(default_factory=list)


class InvitationOut(CamelModel):
    """An invitation as the person being invited sees it."""

    id: uuid.UUID
    role: str
    role_label: str
    business_id: uuid.UUID
    business_name: str
    business_slug: str
    invited_at: datetime | None = None
    expires_at: datetime | None = None


class RoleOut(CamelModel):
    value: str
    label: str
    permissions: list[str]


class BusinessOut(CamelModel):
    """A business as its owner and its team see it."""

    id: uuid.UUID
    name: str
    slug: str
    type: str
    business_type: str | None = None
    business_type_label: str | None = None
    description: str | None = None
    industry: str | None = None
    website: str | None = None
    contact: dict = Field(default_factory=dict)
    social: dict = Field(default_factory=dict)
    logo_url: str | None = None
    cover_url: str | None = None
    verification_status: str
    trust_level: int
    created_at: datetime | None = None


class PublishingIdentityOut(CamelModel):
    """Someone a post can be published under."""

    id: uuid.UUID
    name: str
    type: str
    logo_url: str | None = None
    # The account's own identity. Exactly one entry carries this, and the client
    # selects it without asking when it is the only one.
    is_default: bool = False


class AccountTypeOut(CamelModel):
    """What kind of account this is, and its business when it is one."""

    account_type: str
    # False when the account has never answered - it predates the question, or
    # has only just registered. Distinct from the type itself, which defaults to
    # individual so nothing is ever in an undefined state, and is what lets the
    # interface ask once rather than treating silence as a decision.
    chosen: bool
    business: BusinessOut | None = None


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

"""Identity API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.domains.catalog.schemas import CamelModel

MIN_PASSWORD_LENGTH = 10


class RegisterRequest(CamelModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)
    display_name: str = Field(min_length=1, max_length=120)
    language: str = "en"
    # Lets guest-mode saves and concierge history migrate onto the new account
    # (spec 10.01.01 "Guest Mode").
    anonymous_id: str | None = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, value: str) -> str:
        """Enforce a floor on password strength (spec 10.01.01 "Password Management").

        Length carries most of the entropy, so the composition rule is deliberately
        light: requiring a mix of character classes on top of a 10-character minimum
        mostly teaches users to append "1!".
        """
        if value.strip() != value:
            raise ValueError("Password must not begin or end with whitespace.")
        if value.isdigit() or value.isalpha():
            raise ValueError("Password must combine letters with numbers or symbols.")
        return value


class LoginRequest(CamelModel):
    email: EmailStr
    password: str


class RefreshRequest(CamelModel):
    refresh_token: str


class TokenPair(CamelModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


class ProfileOut(CamelModel):
    user_id: uuid.UUID
    display_name: str
    email: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    language: str
    timezone: str
    home_city_slug: str | None = None
    preferences: dict = Field(default_factory=dict)
    privacy: dict = Field(default_factory=dict)


class MeOut(CamelModel):
    id: uuid.UUID
    status: str
    is_verified: bool
    created_at: datetime
    profile: ProfileOut
    # Exposed so the client knows whether to offer the moderation console. It is
    # a display hint only - every moderation endpoint re-checks the real column,
    # because a flag the client can see is a flag an attacker can forge.
    is_moderator: bool = False


class AuthResponse(CamelModel):
    user: MeOut
    tokens: TokenPair


class UpdateProfileRequest(CamelModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    bio: str | None = Field(default=None, max_length=1000)
    language: str | None = None
    timezone: str | None = None
    home_city_slug: str | None = None
    avatar_url: str | None = None


class PreferencesRequest(CamelModel):
    """Living Explorer Profile update (spec 10.01.02).

    Everything is optional: onboarding asks only for what is immediately useful and
    the profile fills in over time.
    """

    categories: list[str] | None = None
    tags: list[str] | None = None
    disliked_categories: list[str] | None = None
    budget: str | None = None
    travel_purpose: str | None = None
    companions: str | None = None
    accessibility: dict | None = None
    notification: dict | None = None


class PrivacyRequest(CamelModel):
    personalization_enabled: bool | None = None
    location_enabled: bool | None = None
    ai_memory_enabled: bool | None = None
    analytics_enabled: bool | None = None


class SavedItemOut(CamelModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    note: str | None = None
    created_at: datetime

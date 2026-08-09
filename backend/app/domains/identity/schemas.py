"""Identity API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.domains.catalog.schemas import CamelModel

MIN_PASSWORD_LENGTH = 10


def check_password_strength(value: str) -> str:
    """Enforce a floor on password strength (spec 10.01.01 "Password Management").

    Length carries most of the entropy, so the composition rule is deliberately
    light: requiring a mix of character classes on top of a 10-character minimum
    mostly teaches users to append "1!".

    Shared by registration, reset and change rather than duplicated. A password
    floor that applies at signup but not at reset is not a floor - it is a
    formality anyone can step around by asking for a reset link.
    """
    if value.strip() != value:
        raise ValueError("Password must not begin or end with whitespace.")
    if value.isdigit() or value.isalpha():
        raise ValueError("Password must combine letters with numbers or symbols.")
    return value


class RegisterRequest(CamelModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)
    display_name: str = Field(min_length=1, max_length=120)
    language: str = "en"
    # Lets guest-mode saves and concierge history migrate onto the new account
    # (spec 10.01.01 "Guest Mode").
    anonymous_id: str | None = None

    _check_password = field_validator("password")(check_password_strength)


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

    @field_validator("language")
    @classmethod
    def supported_language(cls, value: str | None) -> str | None:
        """Refuse a language nothing is translated into.

        Stored unchecked, an unsupported tag is silent: the explorer picks it,
        every screen stays in English, and there is nothing to tell them the
        setting did not take. Better to say so at the point they set it.

        Normalised too, so `am-ET` and `am` are one preference rather than two -
        Mado has a single translation of Amharic and pretending otherwise is a
        promise about regional variants it cannot keep.
        """
        if value is None:
            return None
        from app.core.language import LANGUAGE_NAMES, normalise

        normalised = normalise(value)
        if normalised is None:
            raise ValueError(f"Supported languages are {', '.join(sorted(LANGUAGE_NAMES))}")
        return normalised


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


# --------------------------------------------------- email and password flows


class EmailRequest(CamelModel):
    email: EmailStr


class ConfirmTokenRequest(CamelModel):
    token: str = Field(min_length=8, max_length=200)


class ResetPasswordRequest(CamelModel):
    token: str = Field(min_length=8, max_length=200)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)

    _check_password = field_validator("password")(check_password_strength)


class ChangePasswordRequest(CamelModel):
    current_password: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)

    _check_password = field_validator("password")(check_password_strength)


class SessionOut(CamelModel):
    """One signed-in device.

    `isCurrent` is what makes the list usable - without it someone trying to
    sign out a device they no longer have has no way to tell which row is the
    browser they are reading this in.
    """

    id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    user_agent: str | None = None
    platform: str | None = None
    is_current: bool = False

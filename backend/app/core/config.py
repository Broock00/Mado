"""Runtime configuration.

Spec 80.01 s16 forbids hardcoded configuration: everything arrives through the
environment so the same image can run in development, QA, staging and production.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MADO_",
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    debug: bool = False

    api_title: str = "Mado Platform API"
    api_version: str = "v1"

    database_url: str = "postgresql+psycopg://mado:mado@localhost:5432/mado"
    redis_url: str = "redis://localhost:6379/0"

    meilisearch_url: str = "http://localhost:7700"
    meilisearch_api_key: str = "mado-dev-master-key"

    jwt_secret: str = "dev-only-change-me-before-any-shared-environment"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 30

    # The gateway owns provider selection; "stub" runs a deterministic local provider
    # so the concierge stays exercisable with no key, no network and no spend.
    ai_provider: Literal["stub", "gemini"] = "stub"
    # Configured separately from chat generation: embeddings are far cheaper, and
    # wanting real semantic retrieval with local generation is a reasonable
    # combination. "auto" follows ai_provider so the default stays unsurprising.
    embedding_provider: Literal["auto", "hashing", "gemini"] = "auto"
    gemini_api_key: str = ""
    # Defaults are verified against the API rather than the model listing, which
    # still advertises gemini-2.0-flash even though generateContent now 404s on it.
    ai_fast_model: str = "gemini-2.5-flash"
    ai_reasoning_model: str = "gemini-2.5-pro"
    ai_request_timeout_seconds: float = 30.0

    # Where uploaded images are written. Local disk in development; spec 82.01
    # names S3-compatible object storage for production, which changes this
    # setting and app/integrations/media_storage.py and nothing else.
    # Maps and geocoding. Spec 82.01 s10 treats maps as infrastructure: the
    # vendor renders tiles and resolves addresses, and Mado owns the catalogue
    # and the ranking. Swapping vendors is therefore a config change.
    #   auto       Google when a key is set, OpenStreetMap otherwise (default)
    #   google     Google Geocoding (requires a key and billing)
    #   nominatim  OpenStreetMap, keyless, rate limited by its operators
    #   local      offline stand-in resolving against known neighbourhoods
    geocoding_provider: Literal["auto", "google", "nominatim", "local"] = "auto"
    google_maps_api_key: str = ""

    # Background maintenance. Popularity, trend and embedding backfill run on a
    # timer inside the API process (spec 70.02 keeps this a single deployable).
    # Off in test runs, where a job firing mid-suite would mutate the data a test
    # is asserting about.
    scheduler_enabled: bool = True

    # Rate limiting. On by default; switched off for test runs, which
    # legitimately create hundreds of accounts and posts from one address in
    # seconds and would otherwise be testing the limiter rather than the feature.
    rate_limit_enabled: bool = True

    # Email. With no host configured the sender writes messages to the log
    # instead, so verification and password reset work end to end in development
    # without a relay - and an operator who forgot to configure one can see
    # exactly what would have gone out rather than discovering silence.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = "Mado <no-reply@mado.local>"

    # Where the links in those emails point. The API cannot infer this - it may
    # sit behind a proxy on a different host from the app the explorer is using.
    web_base_url: str = "http://localhost:5173"

    # Whether publishing requires a confirmed email address.
    #
    # Off by default because it is only meaningful once mail actually sends: with
    # the log sender, turning it on locks every publisher out of the thing they
    # came to do. Production should set MADO_REQUIRE_VERIFIED_EMAIL_TO_PUBLISH=1
    # alongside SMTP - an unverified address is a free, unlimited supply of
    # publishing accounts, which is the cheapest possible spam vector.
    require_verified_email_to_publish: bool = False

    media_root: str = "var/media"
    # Public prefix images are served from. Split from media_root so a CDN can be
    # put in front without moving the files.
    media_base_url: str = ""

    default_city_slug: str = "addis-ababa"

    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()

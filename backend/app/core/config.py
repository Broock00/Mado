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

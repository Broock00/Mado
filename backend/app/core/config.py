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

    # Maps and geocoding. Spec 82.01 s10 treats maps as infrastructure: the
    # vendor renders tiles and resolves addresses, and Mado owns the catalogue
    # and the ranking. Swapping vendors is therefore a config change.
    #   auto       Google when a key is set, OpenStreetMap otherwise (default)
    #   google     Google Geocoding (requires a key and billing)
    #   nominatim  OpenStreetMap, keyless, rate limited by its operators
    #   local      offline stand-in resolving against known neighbourhoods
    geocoding_provider: Literal["auto", "google", "nominatim", "local"] = "auto"
    google_maps_api_key: str = ""

    # Places: turning coordinates into a country/city/street, and a place name
    # back into coordinates. The platform stores no geography of its own, so
    # this is on the read path for every explorer who shares their location.
    #   auto    Google when a key is set, OpenStreetMap otherwise (default)
    #   google  Google Geocoding
    #   osm     OpenStreetMap/Nominatim
    #   stub    resolves nothing; for tests and offline work
    places_provider: Literal["auto", "google", "osm", "stub"] = "auto"
    # Nominatim's operators require an identifying User-Agent and block traffic
    # without one. Put a real contact address here before running in public.
    nominatim_user_agent: str = "Mado/1.0 (city discovery; contact: ops@mado.local)"
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"

    # Routing (spec MAP-002, vendor strategy 82.01). Same shape as geocoding:
    #   auto      Google when a key is set, OSRM otherwise (default)
    #   google    Google Routes
    #   osrm      OpenStreetMap routing
    #   estimate  offline straight-line arithmetic, no network at all
    routing_provider: Literal["auto", "google", "osrm", "estimate"] = "auto"
    # The public demo server is rate limited and not for production use. Point
    # this at a self-hosted instance before relying on it.
    osrm_url: str = "https://router.project-osrm.org"
    # Which profiles the configured OSRM instance actually serves. The public
    # server carries driving only, and asking it to walk returns a driving route
    # with a walking label - worse than an honest estimate.
    osrm_profiles: list[str] = ["drive"]

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

    # Payments (spec COM-003, vendor 82.01: Chapa for Ethiopia).
    #
    # "stub" starts real orders and real holds but never settles one by itself,
    # so a development machine can walk the whole flow without a merchant
    # account and without a stub quietly reporting money that did not arrive.
    payment_provider: Literal["stub", "chapa"] = "stub"
    chapa_secret_key: str = ""
    chapa_base_url: str = "https://api.chapa.co/v1"

    # Stripe is the intended primary once it is switched on, with Chapa kept for
    # birr. Built and tested now, off until then: the flag decides whether the
    # router ever reaches for it, so turning it on is a deployment decision
    # rather than a code change, and turning it off again is too.
    #
    # While this is false, every order goes to whatever `payment_provider` says,
    # which is the behaviour that exists today.
    stripe_enabled: bool = False
    stripe_secret_key: str = ""
    stripe_base_url: str = "https://api.stripe.com/v1"
    # Stripe signs with its own scheme and its own secret, so it does not share
    # MADO_PAYMENT_WEBHOOK_SECRET with Chapa. Empty refuses every callback.
    stripe_webhook_secret: str = ""
    # Used to authenticate the provider's callback. Empty means every callback
    # is refused - see `signature_is_valid`. That is the safe direction: an
    # unsigned callback issues tickets, so a missing setting must not become a
    # way into events for free.
    payment_webhook_secret: str = ""
    # How long an unpaid order holds its places before they go back. Long enough
    # to finish a bank redirect on a slow connection, short enough that a
    # near-full event is not held empty by abandoned carts.
    payment_hold_minutes: int = 20

    # Whether publishing requires a confirmed email address.
    #
    # Off by default because it is only meaningful once mail actually sends: with
    # the log sender, turning it on locks every publisher out of the thing they
    # came to do. Production should set MADO_REQUIRE_VERIFIED_EMAIL_TO_PUBLISH=1
    # alongside SMTP - an unverified address is a free, unlimited supply of
    # publishing accounts, which is the cheapest possible spam vector.
    require_verified_email_to_publish: bool = False

    # Guards /metrics and /health/ready (spec OPERATIONS-42). Between them those
    # describe every dependency, its latency, the shape of the traffic and where
    # the errors are - reconnaissance, if reachable from the internet. With
    # nothing set they answer only outside production, so local work and CI need
    # no setup and a production deployment that forgot fails closed.
    telemetry_token: str = ""

    # Webhook delivery (spec DEV-003). Endpoints are refused if they resolve to
    # a private, loopback or link-local address, which is the SSRF guard - and
    # which also makes a receiver on your own machine untestable. This lifts the
    # check. Development only: switching it on in production turns every webhook
    # subscription into a way to make the API call our own internal services.
    webhook_allow_private_endpoints: bool = False

    # Where uploaded images are written. Local disk in development; spec 82.01
    # names S3-compatible object storage for production, which changes this
    # setting and app/integrations/media_storage.py and nothing else.
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

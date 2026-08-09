"""Mado platform API.

A modular monolith: one deployable, with domain modules that own their own schema
and never reach into each other's tables. The service boundaries drawn in spec
70.02 are preserved as module boundaries so the split into separate services later
is a packaging change rather than a redesign.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.router import api_router
from app.api.routes import platform
from app.core import scheduler
from app.core.config import get_settings
from app.core.context import get_request_id
from app.core.errors import PlatformError
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware
from app.core.rate_limit import RateLimited
from app.core.rate_limit import close as close_rate_limiter
from app.integrations.search import get_search_client

# Registers every domain's models on the shared registry. Required at import time:
# relationships are resolved by class name, so a domain that no route imports
# directly (Publisher, reached only via Venue.publisher) would fail to map.
from app import models  # noqa: F401  isort:skip

settings = get_settings()
logger = get_logger("mado.app")


def _error_body(code: str, message: str, details: object | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "requestId": get_request_id(),
            "timestamp": datetime.now(UTC).isoformat(),
        }
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("api_starting", environment=settings.environment, version=settings.api_version)

    # Index setup is best-effort: search is a discovery accelerator, not a
    # correctness dependency, and spec 55.01 s40 requires graceful degradation.
    search = get_search_client()
    try:
        await search.ensure_indexes()
    except Exception as exc:  # noqa: BLE001
        logger.warning("search_index_setup_failed", error=str(exc))

    scheduler.start()

    yield

    await scheduler.stop()
    await close_rate_limiter()
    logger.info("api_stopping")


app = FastAPI(
    title=settings.api_title,
    version="1.0.0",
    description=(
        "Mado is an AI urban intelligence platform for discovering, planning and "
        "experiencing cities. This is the Explorer-facing API surface."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


@app.exception_handler(PlatformError)
async def platform_error_handler(_: Request, exc: PlatformError) -> JSONResponse:
    # Retry-After on a 429 so a well-behaved client backs off for the right
    # interval instead of guessing - and so retries do not themselves extend the
    # window the caller is waiting on.
    headers = (
        {"Retry-After": str(exc.retry_after)}
        if isinstance(exc, RateLimited)
        else None
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.code, exc.message, exc.details),
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Surface which fields failed without echoing the raw submitted values.
    fields = [
        {"field": ".".join(str(p) for p in err.get("loc", [])[1:]), "issue": err.get("msg", "")}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=_error_body(
            "VALIDATION_FAILED", "The request failed validation.", {"fields": fields}
        ),
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    codes = {
        400: "BAD_REQUEST",
        401: "AUTHENTICATION_REQUIRED",
        403: "PERMISSION_DENIED",
        404: "RESOURCE_NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        409: "RESOURCE_CONFLICT",
        429: "RATE_LIMIT_EXCEEDED",
    }
    code = codes.get(exc.status_code, "REQUEST_FAILED")
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(code, str(exc.detail)),
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # Log the detail, return none of it (spec 80.03 s11).
    logger.exception("unhandled_error", error=str(exc))
    return JSONResponse(
        status_code=500,
        content=_error_body("INTERNAL_ERROR", "An unexpected error occurred."),
    )


# Liveness, readiness and metrics. Outside /api/v1 and unversioned: an
# orchestrator probe does not negotiate an API version, and moving /health in a
# minor release would be a real outage.
app.include_router(platform.router)

app.include_router(api_router, prefix="/api/v1")

# Uploaded images. Served by the application only in development - in production
# this path belongs to a CDN or object store, which is why the URL prefix is
# stable and the storage backend is not baked into it. StaticFiles resolves paths
# against the root and refuses traversal outside it, and every stored name is a
# content hash rather than anything an uploader chose.
_media_root = Path(settings.media_root)
_media_root.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=_media_root), name="media")

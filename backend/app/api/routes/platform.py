"""Operational endpoints (spec OPERATIONS-42).

Not part of the product surface. These are for an orchestrator, a scrape job and
whoever is on call - which is why they sit outside `/api/v1` and are not
versioned: a Kubernetes probe does not negotiate an API version, and moving
`/health` in a minor release would be a genuine outage.

**Readiness and metrics are not public.** Between them they describe every
dependency, its latency, the shape of the traffic and where the errors are -
which is reconnaissance if it is reachable from the internet. Both are gated by
a shared secret, with one deliberate exception: with no token configured, they
answer only outside production. That way local development and CI work with no
setup, and a production deployment that forgot to set the token gets a refusal
rather than an open door.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Response

from app.core import health, metrics
from app.core.config import get_settings
from app.core.errors import PermissionDeniedError

router = APIRouter(tags=["platform"], include_in_schema=False)


def _authorised(token: str | None) -> bool:
    configured = get_settings().telemetry_token
    if not configured:
        # Nothing configured: open in development and test, closed in
        # production and staging. Failing closed matters more than
        # convenience once real traffic is involved.
        return get_settings().environment in {"development", "test"}
    # Constant time: a fast reject on the first wrong byte leaks the token to
    # anybody willing to measure, and this endpoint is a scrape target that
    # tolerates being hit thousands of times.
    return bool(token) and secrets.compare_digest(token, configured)


def _require(token: str | None) -> None:
    if not _authorised(token):
        raise PermissionDeniedError(
            "Not available.", code="TELEMETRY_FORBIDDEN"
        )


@router.get("/health", summary="Liveness")
async def liveness() -> dict:
    """Is this process working?

    Touches nothing external, on purpose. A liveness probe that fails when the
    database is down makes an orchestrator restart every container during a
    database incident, turning one outage into two.
    """
    settings = get_settings()
    return {
        "status": "ok",
        "environment": settings.environment,
        "version": settings.api_version,
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("/health/ready", summary="Readiness")
async def readiness(
    response: Response,
    x_mado_telemetry_token: Annotated[str | None, Header()] = None,
) -> dict:
    """Should traffic come here?

    503 only when something *required* is down. A degraded search index or an
    unreachable model gateway leaves the platform useful, and pulling every
    instance out of the load balancer over an accelerator would be the outage
    the check was meant to prevent.
    """
    _require(x_mado_telemetry_token)

    overall, results = await health.readiness()
    response.status_code = 503 if overall == "unhealthy" else 200
    return {
        "status": overall,
        "checks": [
            {
                "name": r.name,
                "status": r.status,
                "required": r.required,
                "latencyMs": r.latency_ms,
                "detail": r.detail,
            }
            for r in results
        ],
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("/metrics", summary="Prometheus metrics")
async def prometheus_metrics(
    x_mado_telemetry_token: Annotated[str | None, Header()] = None,
) -> Response:
    _require(x_mado_telemetry_token)
    return Response(
        content=metrics.render(),
        # The version parameter is part of the contract, not decoration: a
        # scraper uses it to pick a parser.
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )

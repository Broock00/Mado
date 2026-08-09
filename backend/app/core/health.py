"""Health checks (spec OPERATIONS-42 §5, §10).

Two questions that are always confused and are not the same one:

**Liveness - is this process working?** Answered without touching anything
external. If a liveness probe fails when the database is down, an orchestrator
restarts every API container during a database incident, which turns one outage
into two. A process that can serve a response is alive; whether it can do
anything useful is the other question.

**Readiness - should traffic come here?** Answered by asking each dependency,
and by separating the ones that make the platform useless from the ones that
make it worse. Postgres down means Mado cannot answer anything, so the instance
is not ready. Meilisearch down means search falls back to the database and the
city is still browsable - reporting that as not-ready would pull every healthy
instance out of the load balancer over a degraded accelerator.

That distinction is the whole design here, and it is the same rule the search
integration already follows: degrade, do not fail (spec 55.01 §40).

**Checks are bounded and run together.** Each has its own short timeout and they
run concurrently, so a readiness probe costs the slowest check rather than their
sum, and a hung dependency cannot hold the probe open past the orchestrator's
own timeout - which would be read as a failure of the probe rather than of the
thing it was checking.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import text

from app.core import metrics
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("mado.health")

# Short. A readiness probe runs every few seconds and its job is to notice a
# dependency is gone, not to wait politely for it to come back.
CHECK_TIMEOUT_SECONDS = 2.0

STATUS_UP = "up"
STATUS_DOWN = "down"
# Configured away rather than broken - no API key for the model gateway, no
# Redis URL. Reported honestly instead of as a failure, because an operator who
# turned something off does not want a red light about it.
STATUS_DISABLED = "disabled"
# Configured, but not actually contacted by this probe. Its own status, because
# reporting an unprobed dependency as `up` is the exact failure this file is
# supposed to prevent: a check that always says ok passes every probe and tells
# you nothing. Where a real probe would cost money on every scrape, saying so
# and pointing at the metric that does know is the honest answer.
STATUS_UNKNOWN = "unknown"


@dataclass(slots=True)
class Check:
    name: str
    # False for anything the platform degrades around rather than dies without.
    required: bool
    probe: Callable[[], Awaitable[None]]


@dataclass(slots=True)
class Result:
    name: str
    status: str
    required: bool
    latency_ms: float | None = None
    detail: str | None = None


async def _check_database() -> None:
    from app.core.database import SessionFactory

    async with SessionFactory() as session:
        # `SELECT 1` and nothing else. A probe that queries a real table starts
        # failing when that table is migrated or locked, which is a probe
        # reporting on itself.
        await session.execute(text("SELECT 1"))


async def _check_redis() -> None:
    from app.core.rate_limit import _client

    client = await _client()
    if client is None:
        raise _Disabled("No Redis configured; rate limits fall back to per-process counters.")
    await client.ping()


async def _check_search() -> None:
    from app.integrations.search import get_search_client

    client = get_search_client()
    if not await client.health():
        raise RuntimeError("Meilisearch did not answer its health check.")


async def _check_ai() -> None:
    settings = get_settings()
    if settings.ai_provider == "stub":
        raise _Disabled("Running the deterministic local provider; no model is called.")
    if not settings.gemini_api_key:
        raise _Disabled("No API key configured.")
    # Deliberately not a generation call: a readiness probe that spends money
    # every few seconds is a bill. So this reports `unknown` rather than `up` -
    # claiming a dependency is healthy without having asked it is how a health
    # check becomes decoration. The failure it would have caught, a revoked key,
    # appears in `mado_dependency_calls_total` on the first real request.
    raise _Unprobed(
        "Configured but not contacted - a probe would cost a model call. "
        "See mado_dependency_calls_total{dependency=\"ai\"}."
    )


class _Disabled(Exception):
    """Not an error: this dependency is switched off on purpose."""


class _Unprobed(Exception):
    """Not an error, and not a clean bill of health either: nothing was asked."""


CHECKS: tuple[Check, ...] = (
    # Nothing works without it. Every read and every write goes here.
    Check("database", True, _check_database),
    # Rate limits fall back to per-process counters, which is wrong across
    # workers but not fatal; the scheduler declines to run rather than running
    # everywhere. Both are degradations, neither is an outage.
    Check("redis", False, _check_redis),
    # Search falls back to the database.
    Check("search", False, _check_search),
    # The concierge stops; browsing, planning and publishing do not.
    Check("ai", False, _check_ai),
)


async def _run(check: Check) -> Result:
    started = time.perf_counter()
    try:
        await asyncio.wait_for(check.probe(), timeout=CHECK_TIMEOUT_SECONDS)
    except _Disabled as exc:
        return Result(check.name, STATUS_DISABLED, check.required, detail=str(exc))
    except _Unprobed as exc:
        return Result(check.name, STATUS_UNKNOWN, check.required, detail=str(exc))
    except TimeoutError:
        elapsed = (time.perf_counter() - started) * 1000
        return Result(
            check.name,
            STATUS_DOWN,
            check.required,
            round(elapsed, 1),
            f"No answer within {CHECK_TIMEOUT_SECONDS:g}s.",
        )
    except Exception as exc:  # noqa: BLE001 - any failure is "down"
        elapsed = (time.perf_counter() - started) * 1000
        # The message is for an operator reading a probe response, so it says
        # what failed. It is not shown to explorers - `/health/ready` is not on
        # the public surface (see `app/api/routes/platform.py`).
        return Result(check.name, STATUS_DOWN, check.required, round(elapsed, 1), str(exc)[:200])

    elapsed = (time.perf_counter() - started) * 1000
    return Result(check.name, STATUS_UP, check.required, round(elapsed, 1))


async def readiness() -> tuple[str, list[Result]]:
    """Ask everything at once. Returns an overall status and the detail.

    - `ok`        everything required is up
    - `degraded`  something optional is down; still serve traffic
    - `unhealthy` something required is down; do not send traffic here
    """
    results = await asyncio.gather(*(_run(check) for check in CHECKS))

    for result in results:
        # Only up and down get a gauge. A disabled dependency reported as 0
        # would page somebody about a setting, and an unprobed one reported as
        # either number would be a guess dressed as a measurement.
        if result.status in {STATUS_UP, STATUS_DOWN}:
            metrics.dependency_up.set(1 if result.status == STATUS_UP else 0, result.name)

    if any(r.required and r.status == STATUS_DOWN for r in results):
        overall = "unhealthy"
    elif any(r.status == STATUS_DOWN for r in results):
        overall = "degraded"
    else:
        overall = "ok"

    if overall != "ok":
        logger.warning(
            "readiness_degraded",
            overall=overall,
            down=[r.name for r in results if r.status == STATUS_DOWN],
        )
    return overall, list(results)

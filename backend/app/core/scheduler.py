"""Background maintenance.

Some of the platform's numbers are derived rather than stored: popularity and
trend come from the interaction stream, and embeddings have to be generated for
anything published while the embedding provider was unavailable. Both were
CLI-only, which in practice means they run when someone remembers - and a
Trending rail computed last Tuesday is worse than no Trending rail, because it
looks current.

An in-process asyncio loop rather than a separate worker or a cron container.
This is a modular monolith by design (spec 70.02): adding a second deployable to
run one query every half hour would be a real operational cost for no benefit,
and the job is small, idempotent and safe to miss.

**Only one process may run the jobs.** With several workers behind a load
balancer, every one of them would otherwise wake up and recompute the same
scores. A Redis lock elects a single runner per interval; without Redis the loop
does not run at all rather than running everywhere, because duplicated writes are
worse than stale numbers.

Nothing here is on a request path, so every failure is logged and swallowed. A
maintenance job that takes the API down with it has done more damage than the
stale data it was fixing.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core import metrics
from app.core.config import get_settings
from app.core.database import SessionFactory
from app.core.logging import get_logger
from app.core.rate_limit import _client as redis_client

logger = get_logger("mado.scheduler")

# How often popularity and trend are rebuilt. Half-hourly is far more often than
# the numbers meaningfully move, and cheap enough that it does not matter - the
# point is that "trending" never means "trending last week".
ENGAGEMENT_INTERVAL_SECONDS = 1800

# Reminders are built and delivered on the same short cycle. Short because the
# lead time is three hours and a longer cycle would let an event slip inside that
# window between runs - a reminder that arrives after the concert is worse than
# none, because it teaches an explorer to ignore the next one.
REMINDER_INTERVAL_SECONDS = 600

# Embedding backfill catches anything published while the provider was down.
# Hourly: a listing without an embedding is still fully discoverable by keyword,
# so this is a quality repair rather than an outage.
EMBEDDING_INTERVAL_SECONDS = 3600

# Webhook delivery. The shortest interval here by a distance: a webhook that
# arrives ten minutes after the thing it describes is not a notification, it is
# a report. Half a minute is the difference between "your listing went live" and
# "your listing went live a while ago", and each run is one indexed query that
# usually returns nothing.
WEBHOOK_INTERVAL_SECONDS = 30

# Delivery history is trimmed daily rather than per delivery, which would put a
# DELETE on the send path for no benefit.
WEBHOOK_SWEEP_INTERVAL_SECONDS = 86400

# A held lock expires slightly after the interval it guards, so a worker that
# dies mid-job does not block the next run forever.
LOCK_MARGIN_SECONDS = 60


# Publisher reputation (spec TRST-004). Hourly, because the evidence it reads -
# a date that went ahead, a rating, a report - accumulates over days, and a
# reputation that moved within the hour of a single cancellation would be
# reacting to noise rather than describing a record.
REPUTATION_INTERVAL_SECONDS = 3600

# How often each process re-checks that it can still reach its dependencies.
# Frequent, because it is four small round trips and it is what keeps
# `mado_dependency_up` a live signal rather than a stale one; not so frequent
# that a struggling database gets a probe every second on top of real traffic.
HEALTH_INTERVAL_SECONDS = 30


@dataclass(slots=True)
class Job:
    name: str
    interval_seconds: int
    run: Callable[[], Awaitable[None]]
    # Shared work is elected: one worker recomputes trend, one sends the
    # webhooks, because doing it twice means duplicate writes and duplicate
    # deliveries. A *local* job is the opposite - it observes this process, so
    # every process has to run it, and electing a leader would leave every other
    # instance reporting nothing about itself.
    local: bool = False


async def _claim(name: str, ttl: int) -> bool:
    """Elect a single runner for this interval.

    SET NX EX: the first worker to claim the key wins, and the key expires on its
    own so a crashed holder cannot deadlock the job. Without Redis nobody runs -
    see the module docstring.
    """
    client = await redis_client()
    if client is None:
        return False
    try:
        return bool(await client.set(f"job:{name}", "held", nx=True, ex=ttl))
    except Exception as exc:  # noqa: BLE001 - never fail a background loop
        logger.warning("scheduler_lock_failed", job=name, error=str(exc))
        return False


async def _recompute_engagement() -> None:
    from app.domains.explorer.learning import recompute_engagement_scores

    async with SessionFactory() as session:
        result = await recompute_engagement_scores(session)
        await session.commit()
        logger.info("scheduled_engagement_recompute", **result)


async def _backfill_embeddings() -> None:
    from app.domains.discovery.embedding_service import backfill_embeddings

    async with SessionFactory() as session:
        written = await backfill_embeddings(session, only_missing=True)
        await session.commit()
        if written:
            logger.info("scheduled_embedding_backfill", written=written)


async def _reminders() -> None:
    from app.domains.explorer.reminders import (
        deliver_due,
        schedule_event_reminders,
        schedule_plan_reminders,
    )

    async with SessionFactory() as session:
        events = await schedule_event_reminders(session)
        plans = await schedule_plan_reminders(session)
        delivered = await deliver_due(session)
        await session.commit()

        if events["created"] or plans["created"] or delivered:
            logger.info(
                "scheduled_reminders",
                event_reminders=events["created"],
                plan_reminders=plans["created"],
                delivered=delivered,
            )


async def _reputation() -> None:
    from app.domains.trust.reputation import refresh_all

    async with SessionFactory() as session:
        result = await refresh_all(session)
        await session.commit()
        logger.info("scheduled_reputation_refresh", **result)


async def _webhooks() -> None:
    from app.domains.developer.delivery import deliver_due

    async with SessionFactory() as session:
        result = await deliver_due(session)
        await session.commit()
        if result["attempted"]:
            logger.info("scheduled_webhook_delivery", **result)


async def _sweep_deliveries() -> None:
    from app.domains.developer.retention import forget_old_deliveries

    async with SessionFactory() as session:
        removed = await forget_old_deliveries(session)
        await session.commit()
        if removed:
            logger.info("scheduled_webhook_sweep", removed=removed)


async def _refresh_health() -> None:
    """Re-check every dependency so `mado_dependency_up` stays a live signal.

    Without this the gauge is only written when something polls
    `/health/ready`, which means a Prometheus scraper reading `/metrics` sees
    the series appear, disappear, or never exist at all depending on whether
    anybody happened to call a different endpoint. A dependency-down alert that
    only fires when somebody is already looking is not an alert.
    """
    from app.core.health import readiness

    await readiness()


JOBS = [
    Job("health", HEALTH_INTERVAL_SECONDS, _refresh_health, local=True),
    Job("engagement", ENGAGEMENT_INTERVAL_SECONDS, _recompute_engagement),
    Job("embeddings", EMBEDDING_INTERVAL_SECONDS, _backfill_embeddings),
    Job("reputation", REPUTATION_INTERVAL_SECONDS, _reputation),
    Job("reminders", REMINDER_INTERVAL_SECONDS, _reminders),
    Job("webhooks", WEBHOOK_INTERVAL_SECONDS, _webhooks),
    Job("webhook_sweep", WEBHOOK_SWEEP_INTERVAL_SECONDS, _sweep_deliveries),
]


async def _loop(job: Job) -> None:
    # Shared jobs wait before their first run rather than after. Starting every
    # one at boot would make a deploy - when several workers start at once - the
    # busiest moment on the database.
    #
    # A local job runs immediately instead. Its whole purpose is to say
    # something about this process, and an instance that reports nothing about
    # itself for the first half minute after starting is silent during exactly
    # the window a deploy goes wrong in.
    first = job.local
    while True:
        if not first:
            await asyncio.sleep(job.interval_seconds)
        first = False
        try:
            if not job.local and not await _claim(
                job.name, job.interval_seconds + LOCK_MARGIN_SECONDS
            ):
                # Counted, so "this worker never wins the lock" is visible.
                # Without it, a job that stopped running and a job that lost
                # every election look identical from outside.
                metrics.job_runs.inc(job.name, "skipped")
                continue
            started = datetime.now(UTC)
            await job.run()
            elapsed = (datetime.now(UTC) - started).total_seconds()
            metrics.job_runs.inc(job.name, "ok")
            metrics.job_duration.observe(elapsed, job.name)
            logger.info("scheduled_job_complete", job=job.name, seconds=round(elapsed, 2))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a job must never stop the loop
            # Background work is invisible in request metrics, which is exactly
            # why a job that has started failing needs its own counter: nothing
            # else would notice.
            metrics.job_runs.inc(job.name, "error")
            logger.warning("scheduled_job_failed", job=job.name, error=str(exc))


_tasks: list[asyncio.Task] = []


def start() -> None:
    """Begin the maintenance loops."""
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info("scheduler_disabled")
        return

    for job in JOBS:
        _tasks.append(asyncio.create_task(_loop(job), name=f"mado-job-{job.name}"))
    logger.info("scheduler_started", jobs=[job.name for job in JOBS])


async def stop() -> None:
    """Cancel the loops on shutdown, so a reload does not leave them running."""
    for task in _tasks:
        task.cancel()
    for task in _tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _tasks.clear()

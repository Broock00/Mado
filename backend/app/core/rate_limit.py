"""Rate limiting.

Open publishing means anyone with an account can write to the catalogue, and
publishing now costs a model call for screening. That makes an unlimited write
path two problems at once: a spam vector and a spend vector. This is the control
for both.

**Sliding window, not fixed buckets.** A fixed window resets on a clock boundary,
so a limit of 10/hour actually permits 20 requests in the two minutes either side
of the hour. The sorted-set approach here counts what happened in the *trailing*
window, which is what the limit is meant to express.

**Redis first, memory as a fallback.** Limits have to hold across workers, and an
in-process counter silently multiplies every limit by the number of processes. The
fallback exists so local development works without Redis running, and it says so
in the logs rather than pretending to be the real thing.

**Fail open.** If Redis is unreachable the request proceeds. A rate limiter that
takes the whole site down when its backing store hiccups has caused a worse outage
than the abuse it was guarding against - and the deterministic screening,
moderation queue and reporting paths all still apply underneath.

Identity is the account where there is one and the client address otherwise.
Anonymous limits are necessarily coarser: several people behind one NAT share an
address, so those limits are set loose enough not to punish them, and the tight
limits sit on the authenticated actions that actually create content.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

import redis.asyncio as aioredis
from fastapi import Request

from app.core import metrics
from app.core.config import get_settings
from app.core.errors import PlatformError
from app.core.logging import get_logger

logger = get_logger("mado.rate_limit")


@dataclass(frozen=True, slots=True)
class Limit:
    """A number of requests per trailing window."""

    times: int
    seconds: int
    # Names the bucket in Redis, so two endpoints sharing a limit definition do
    # not consume each other's allowance.
    scope: str

    @property
    def description(self) -> str:
        if self.seconds % 3600 == 0:
            unit = f"{self.seconds // 3600} hour" if self.seconds > 3600 else "hour"
        elif self.seconds % 60 == 0:
            unit = f"{self.seconds // 60} minutes" if self.seconds > 60 else "minute"
        else:
            unit = f"{self.seconds} seconds"
        return f"{self.times} per {unit}"


# Publishing is the expensive path: it writes to the catalogue, reindexes, embeds
# and runs two screeners including a model call. Generous for a real publisher -
# nobody writes twenty genuine listings an hour - and ruinous for a script.
PUBLISH_LIMIT = Limit(times=20, seconds=3600, scope="publish")

# Creating drafts is cheap and iterative, so this sits well above publishing.
DRAFT_LIMIT = Limit(times=60, seconds=3600, scope="draft")

# Each concierge turn costs a comprehension call plus a generation call.
CONCIERGE_LIMIT = Limit(times=60, seconds=3600, scope="concierge")

# Reports are a safety mechanism and must stay easy to use. This is high enough
# that a diligent reader never meets it, and low enough to blunt a brigading
# script - which the report threshold and the "humans decide" rule already blunt.
REPORT_LIMIT = Limit(times=30, seconds=3600, scope="report")

# Registration from one address. Deliberately loose: shared NAT is the norm, not
# the exception - an office, a university or a cafe all present one address, and a
# tight limit there locks out a building to inconvenience a script that can rent a
# new address for pennies. The real defences against fake accounts are email
# verification and the screening that every post goes through, not this.
REGISTER_LIMIT = Limit(times=60, seconds=3600, scope="register")

# Sign-in attempts from one address. The conventional balance: enough to survive
# a few genuine mistakes and a shared office connection, low enough that credential
# stuffing from a single address is not worth attempting. Not the numerically
# smallest limit here - it does not need to be, because Argon2 hashing already
# makes each attempt expensive for the attacker rather than for us.
LOGIN_LIMIT = Limit(times=10, seconds=900, scope="login")

# Verification requests. Each one lands in a human queue, so the limit exists to
# stop a queue being flooded rather than to control spend.
VERIFICATION_LIMIT = Limit(times=5, seconds=86400, scope="verification")

# Reviews. Each one costs a screening call, and a genuine explorer reviews the
# handful of places they actually went - not thirty an hour.
REVIEW_LIMIT = Limit(times=30, seconds=3600, scope="review")

# Image uploads are decoded and re-encoded server-side, which is CPU-bound. This
# is the one limit protecting a synchronous compute path rather than a spend path.
UPLOAD_LIMIT = Limit(times=40, seconds=3600, scope="upload")

# Geocoding calls a third party that rate limits us in turn - OpenStreetMap asks
# for roughly one request a second across all of our traffic, so this protects
# their service as much as ours.
GEOCODE_LIMIT = Limit(times=60, seconds=3600, scope="geocode")

# Planning runs a full retrieval and solve, so it is heavier than a page view.
PLAN_LIMIT = Limit(times=60, seconds=3600, scope="plan")


class RateLimited(PlatformError):
    """429. Carries the retry hint so a client can back off rather than hammer."""

    status_code = 429
    code = "RATE_LIMITED"
    message = "Too many requests."

    def __init__(self, limit: Limit, retry_after: int) -> None:
        super().__init__(
            f"Too many requests. The limit is {limit.description}.",
            details={"retryAfter": retry_after, "limit": limit.description},
        )
        self.retry_after = retry_after


class _InProcessLimiter:
    """Fallback used when Redis is unavailable.

    Correct within one process and wrong across several, which is exactly why it
    is not the default. It keeps local development working rather than making
    Redis a prerequisite for running the app.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: Limit, now: float) -> int | None:
        window = self._hits[key]
        cutoff = now - limit.seconds
        while window and window[0] <= cutoff:
            window.popleft()

        if len(window) >= limit.times:
            return max(1, int(window[0] + limit.seconds - now))

        window.append(now)
        return None


_fallback = _InProcessLimiter()
_redis: aioredis.Redis | None = None
_redis_unavailable = False


async def _client() -> aioredis.Redis | None:
    global _redis, _redis_unavailable
    if _redis_unavailable:
        return None
    if _redis is None:
        try:
            _redis = aioredis.from_url(
                get_settings().redis_url, encoding="utf-8", decode_responses=True
            )
            await _redis.ping()
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the request
            logger.warning("rate_limit_redis_unavailable_using_memory", error=str(exc))
            _redis_unavailable = True
            _redis = None
    return _redis


async def check(identity: str, limit: Limit) -> None:
    """Consume one unit of allowance, or raise :class:`RateLimited`.

    Implemented as a sorted set keyed by timestamp: drop everything older than
    the window, count what remains, and add this request. Executed in a pipeline
    so the read and the write cannot interleave with another worker's.
    """
    if not get_settings().rate_limit_enabled:
        return

    now = time.time()
    key = f"ratelimit:{limit.scope}:{identity}"

    client = await _client()
    if client is None:
        retry_after = _fallback.check(key, limit, now)
        if retry_after is not None:
            raise RateLimited(limit, retry_after)
        return

    cutoff = now - limit.seconds
    try:
        pipe = client.pipeline()
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zcard(key)
        pipe.zadd(key, {f"{now}:{id(now)}": now})
        # Expiry is reset on every write so an idle key disappears on its own
        # rather than accumulating for every explorer who ever visited.
        pipe.expire(key, limit.seconds)
        _, count, _, _ = await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        # Fail open. An unreachable limiter must not become an outage.
        logger.warning("rate_limit_check_failed_allowing", error=str(exc), scope=limit.scope)
        return

    if count >= limit.times:
        # The request was already recorded above, which is deliberate: a caller
        # that keeps hammering keeps extending their own window rather than
        # getting a free retry the moment the oldest entry expires.
        try:
            oldest = await client.zrange(key, 0, 0, withscores=True)
            retry_after = (
                max(1, int(oldest[0][1] + limit.seconds - now)) if oldest else limit.seconds
            )
        except Exception:  # noqa: BLE001
            retry_after = limit.seconds

        # Counted as well as logged: a rising number here is either abuse or a
        # limit set too tight, and only a graph over time tells them apart.
        metrics.rate_limited.inc(limit.scope)
        logger.info(
            "rate_limited", scope=limit.scope, identity=identity[:16], retry_after=retry_after
        )
        raise RateLimited(limit, retry_after)


def identify(request: Request, user_id: str | None = None) -> str:
    """The subject of a limit.

    An account where one exists, because that is the durable identity and the one
    that carries accountability. Otherwise the client address, accepting that
    several people can share one.
    """
    if user_id:
        return f"user:{user_id}"

    # X-Forwarded-For is only trustworthy behind a proxy that sets it. Taking the
    # left-most entry is standard, but it is client-controlled, so this is used
    # for anonymous limits only - never for anything security-critical.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


async def close() -> None:
    """Release the connection on shutdown."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None

# Emails sent to an address the requester merely typed. This is the one limit
# that protects a third party rather than the platform: without it, the
# forgot-password form is a way to have Mado repeatedly mail someone else's
# inbox. Keyed on the address, not the caller, so rotating IPs does not help.
EMAIL_SEND_LIMIT = Limit(times=5, seconds=3600, scope="email_send")

# Attempts to spend a token from a link. Low, because a legitimate explorer
# clicks a link once and anyone trying many is guessing.
TOKEN_CONFIRM_LIMIT = Limit(times=20, seconds=3600, scope="token_confirm")

# Calls made with an API key. Higher than any human limit, because the point of
# a key is to run a script - a nightly sync of a season's events is hundreds of
# calls in a minute and is exactly the use case. Keyed on the key rather than
# the account, so a runaway integration exhausts its own allowance instead of
# locking its owner out of the website (spec 55.01 s25: partner limits are their
# own tier).
API_KEY_LIMIT = Limit(times=1000, seconds=3600, scope="apikey")

# Reservations. Higher than publishing because changing your mind about a party
# size is normal, and low enough that a script cannot exhaust a supper club's
# seats by holding and releasing them in a loop.
RESERVE_LIMIT = Limit(times=30, seconds=3600, scope="reserve")

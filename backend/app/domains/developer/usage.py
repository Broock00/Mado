"""Counting API calls, and pricing them.

BUSINESS-09 groups enterprise and API at 25% of the long-term revenue mix, and
BUSINESS-90.01 §13 asks for a free developer tier with usage-based pricing above
it. Keys and scopes already existed; nothing counted a call, so there was no
figure to bill from and no way to tell a runaway integration from a successful
one.

**Daily rollups, not a row per request.** A row per call is a firehose that
nobody bills from and everybody has to prune - and the question being answered
is "how many this month", which a counter answers exactly as well and a table
scan answers slowly. The day is UTC so a month is the same length for everybody,
which is not true of any local calendar.

**The plan belongs to the account, not to the key.** On the key, minting a
second one would mint a second allowance, which is a free upgrade in the shape
of a button that already exists.

**A quota is not a rate limit.** `core/rate_limit.py` protects the service from
a caller going too fast, and answers "slow down". This says the caller has used
what they bought, and answers "buy more". They are separate numbers with
separate remedies, so they get separate codes: a developer who cannot tell them
apart will back off from one and never fix the other.

**Counting must never fail a call.** If the counter cannot be written the
request proceeds, exactly as `rate_limit` allows a call through when Redis is
unreachable. Losing a count costs a fraction of an invoice; losing the request
costs the customer's integration.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Date, ForeignKey, Index, Integer, String, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.errors import PlatformError
from app.core.logging import get_logger
from app.core.mixins import Timestamps, UUIDPrimaryKey

logger = get_logger("mado.api_usage")

SCHEMA = "publisher"

# --- Plans -------------------------------------------------------------------
#
# Held here rather than in `publisher/plans.py`, because these are sold to a
# different customer for a different thing. A hotel buying Professional is
# buying somewhere to publish; a travel startup buying Pro here is buying
# requests. Merging them would force one of the two to carry fields that mean
# nothing to it.

PLAN_FREE = "free"
PLAN_PRO = "pro"
PLAN_ENTERPRISE = "enterprise"


@dataclass(frozen=True, slots=True)
class DeveloperPlan:
    key: str
    name: str
    # Calls included per calendar month. None is unmetered, which is what an
    # enterprise contract actually buys - the agreement is the limit.
    monthly_calls: int | None
    prices_minor: dict[str, int]


DEVELOPER_PLANS: dict[str, DeveloperPlan] = {
    PLAN_FREE: DeveloperPlan(
        key=PLAN_FREE,
        name="Free",
        # Enough to build something and see it work. A free tier that runs out
        # during an afternoon's integration work teaches a developer that the
        # API is a liability, and they do not come back to buy the paid one.
        monthly_calls=10_000,
        prices_minor={},
    ),
    PLAN_PRO: DeveloperPlan(
        key=PLAN_PRO,
        name="Pro",
        monthly_calls=1_000_000,
        prices_minor={"ETB": 500_000, "USD": 9_900, "EUR": 9_900, "GBP": 8_500},
    ),
    PLAN_ENTERPRISE: DeveloperPlan(
        key=PLAN_ENTERPRISE,
        name="Enterprise",
        monthly_calls=None,
        prices_minor={},
    ),
}


def plan_for(key: str | None) -> DeveloperPlan:
    """Fails closed to free, for the reason every other vocabulary here does."""
    return DEVELOPER_PLANS.get(key or PLAN_FREE, DEVELOPER_PLANS[PLAN_FREE])


class QuotaExceeded(PlatformError):
    """The allowance is spent - which is not the same as going too fast.

    429 is right for both, so the code is what distinguishes them. A developer
    told only "429" backs off and waits, which fixes a rate limit and does
    nothing at all about a quota.
    """

    status_code = 429
    code = "QUOTA_EXCEEDED"


# --- Tables ------------------------------------------------------------------


class DeveloperAccount(Base, UUIDPrimaryKey, Timestamps):
    """What one account may spend on API calls.

    No row means the free plan, the same way no subscription means the free
    publisher plan: the majority never buy anything and a model requiring a row
    for them is a migration somebody forgets.
    """

    __tablename__ = "developer_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_developer_account_user"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("identity.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan: Mapped[str] = mapped_column(String(32), default=PLAN_FREE, nullable=False)
    # An allowance agreed with one customer, overriding the plan's. Enterprise
    # contracts are negotiated, and encoding each one as a new plan would mean a
    # deploy per customer.
    monthly_calls_override: Mapped[int | None] = mapped_column(Integer, default=None)


class ApiUsage(Base, UUIDPrimaryKey, Timestamps):
    """One key, one UTC day, one counter."""

    __tablename__ = "api_usage"
    __table_args__ = (
        UniqueConstraint("api_key_id", "day", name="uq_api_usage_key_day"),
        # Summing a month for one account, which is what a bill is.
        Index("ix_api_usage_owner_day", "owner_user_id", "day"),
        {"schema": SCHEMA},
    )

    api_key_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Denormalised so a month's total for an account is one indexed scan rather
    # than a join through keys that may since have been revoked - a revoked key's
    # calls still happened and are still billable.
    owner_user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# --- Counting ----------------------------------------------------------------


def month_start(now: datetime | None = None) -> date:
    today = (now or datetime.now(UTC)).date()
    return today.replace(day=1)


def next_month_start(now: datetime | None = None) -> date:
    """When the allowance resets.

    By arithmetic on the month number rather than by adding thirty days, which
    lands in the wrong month twice a year and never in February.
    """
    first = month_start(now)
    return (
        first.replace(year=first.year + 1, month=1)
        if first.month == 12
        else first.replace(month=first.month + 1)
    )


async def account_for(
    session: AsyncSession, user_id: uuid.UUID
) -> DeveloperAccount | None:
    return (
        await session.execute(
            select(DeveloperAccount).where(DeveloperAccount.user_id == user_id)
        )
    ).scalar_one_or_none()


async def allowance_for(session: AsyncSession, user_id: uuid.UUID) -> int | None:
    """Calls included this month, or None for unmetered."""
    account = await account_for(session, user_id)
    if account is None:
        return plan_for(PLAN_FREE).monthly_calls
    if account.monthly_calls_override is not None:
        return account.monthly_calls_override
    return plan_for(account.plan).monthly_calls


async def calls_this_month(
    session: AsyncSession, user_id: uuid.UUID, *, now: datetime | None = None
) -> int:
    total = (
        await session.execute(
            select(func.coalesce(func.sum(ApiUsage.calls), 0)).where(
                ApiUsage.owner_user_id == user_id,
                ApiUsage.day >= month_start(now),
            )
        )
    ).scalar_one()
    return int(total)


async def daily_breakdown(
    session: AsyncSession, user_id: uuid.UUID, *, days: int = 31
) -> list[tuple[date, int]]:
    since = datetime.now(UTC).date() - timedelta(days=days)
    rows = (
        await session.execute(
            select(ApiUsage.day, func.sum(ApiUsage.calls))
            .where(ApiUsage.owner_user_id == user_id, ApiUsage.day >= since)
            .group_by(ApiUsage.day)
            .order_by(ApiUsage.day)
        )
    ).all()
    return [(day, int(calls)) for day, calls in rows]


async def record_call(
    session: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    now: datetime | None = None,
) -> None:
    """Add one to today's counter for this key.

    An upsert rather than a read-then-write: two calls on the same key land in
    the same millisecond routinely, and the database is the only thing that can
    order them. Failure is logged and swallowed - see the module docstring.
    """
    today = (now or datetime.now(UTC)).date()
    statement = (
        insert(ApiUsage)
        .values(api_key_id=api_key_id, owner_user_id=owner_user_id, day=today, calls=1)
        .on_conflict_do_update(
            constraint="uq_api_usage_key_day",
            set_={"calls": ApiUsage.calls + 1},
        )
    )
    try:
        await session.execute(statement)
    except Exception as exc:  # noqa: BLE001 - a counter must never fail a request
        logger.warning("api_usage_record_failed", error=str(exc))


async def assert_within_quota(
    session: AsyncSession, user_id: uuid.UUID, *, now: datetime | None = None
) -> None:
    allowance = await allowance_for(session, user_id)
    if allowance is None:
        return
    used = await calls_this_month(session, user_id, now=now)
    if used >= allowance:
        raise QuotaExceeded(
            f"This month's {allowance:,} API calls are used up. The allowance resets "
            f"on the first, or a larger plan raises it.",
            details={"used": used, "allowance": allowance},
        )


__all__ = [
    "DEVELOPER_PLANS",
    "PLAN_ENTERPRISE",
    "PLAN_FREE",
    "PLAN_PRO",
    "ApiUsage",
    "DeveloperAccount",
    "DeveloperPlan",
    "QuotaExceeded",
    "account_for",
    "allowance_for",
    "assert_within_quota",
    "calls_this_month",
    "daily_breakdown",
    "month_start",
    "next_month_start",
    "plan_for",
    "record_call",
]

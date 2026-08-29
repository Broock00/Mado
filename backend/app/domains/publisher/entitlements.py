"""Whether the account bought this - which is not whether the person may do it.

Mado has one authorization gate, `PublishingService.assert_can_manage`, and this
is deliberately **not** part of it. The two questions are unrelated and both have
to be answered:

- `permissions_for` asks *may this person do this to this business* - a fact
  about a role.
- `entitlements_for` asks *did this account pay for this* - a fact about a plan.

Folding the second into the first would make one refusal mean two things. An
editor told "your role does not allow this" when the business is simply on the
free tier would go and ask for a bigger role, get it, and be refused again;
whoever granted it would have widened somebody's access for nothing. So a plan
limit raises its own error, naming the plan that would allow it, and the
interface can put the upgrade where the refusal happened.

**A downgrade never deletes anything.** An account that falls below its limits
keeps everything it has and is blocked from adding more. Withdrawing listings
because somebody stopped paying would destroy their work, break every saved item
and repost pointing at it, and take down events explorers hold tickets to.
Nothing here removes; it only refuses.

**An expired period grants nothing, and the row is left alone.** The plan is
read through the period, so a subscription that ended yesterday is free today
without anything having to run on a timer. A sweep that rewrote the row would be
one missed run away from an account keeping what it stopped paying for.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PlatformError
from app.domains.publisher import plans
from app.domains.publisher.models import (
    MEMBERSHIP_ACTIVE,
    MEMBERSHIP_INVITED,
    PublisherMember,
    Subscription,
)


class PlanLimitError(PlatformError):
    """This account's plan does not stretch to it.

    Its own class rather than a `PermissionDeniedError` with a different
    message, because the client branches on it: this is the one refusal that
    should offer an upgrade rather than an explanation of roles.

    402 Payment Required is the honest status. 403 would say the caller may not
    do this, which is wrong - they may, and the account has not bought it.
    """

    status_code = 402
    code = "PLAN_LIMIT"


def _upgrade_to(needed: str) -> str:
    """The cheapest plan that would lift this limit, for the message."""
    return plans.PLANS[needed].name


async def plan_of(session: AsyncSession, publisher_id: uuid.UUID) -> plans.Plan:
    """What this business is on, right now.

    Free when there is no row, when the status does not grant, and when the
    period has run out. All three are the same answer because they are the same
    situation: nothing has been paid for that is still in force.
    """
    subscription = (
        await session.execute(
            select(Subscription).where(Subscription.publisher_id == publisher_id)
        )
    ).scalar_one_or_none()
    if subscription is None:
        return plans.get(plans.FREE)
    if subscription.status not in plans.STATUS_GRANTING:
        return plans.get(plans.FREE)

    ends = subscription.current_period_end
    if ends is None:
        # No end date is an open-ended grant - an enterprise agreement, which
        # ends when somebody ends it rather than when a date passes unnoticed.
        # A cancelled subscription never reaches here: `cancel` always leaves a
        # date, so cancelling cannot produce a plan that grants forever.
        return plans.get(subscription.plan)

    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=UTC)
    if ends <= datetime.now(UTC):
        return plans.get(plans.FREE)

    return plans.get(subscription.plan)


async def entitlements_for(
    session: AsyncSession, publisher_id: uuid.UUID
) -> plans.Entitlements:
    return (await plan_of(session, publisher_id)).entitlements


async def assert_can_publish_another(
    session: AsyncSession, publisher_id: uuid.UUID
) -> None:
    """Refuse a sixth live listing on a plan that allows five.

    Counts what is **live**, not what exists. A draft costs the platform nothing
    and stopping somebody writing would push the work elsewhere rather than sell
    them anything - so the limit binds at the moment of publishing, which is
    also the moment the value being paid for is delivered.
    """
    entitlements = await entitlements_for(session, publisher_id)
    if entitlements.max_live_listings is None:
        return

    # Imported here rather than at module scope: catalog imports publisher for
    # its foreign keys, and taking the dependency the other way round at import
    # time closes the loop.
    from app.domains.catalog.models import STATUS_PUBLISHED, Experience

    live = (
        await session.execute(
            select(func.count())
            .select_from(Experience)
            .where(
                Experience.publisher_id == publisher_id,
                Experience.status == STATUS_PUBLISHED,
                Experience.deleted_at.is_(None),
            )
        )
    ).scalar_one()

    if live >= entitlements.max_live_listings:
        raise PlanLimitError(
            f"This plan keeps {entitlements.max_live_listings} posts live at once, and "
            f"you have {live}. Withdraw one, or move to "
            f"{_upgrade_to(plans.PROFESSIONAL)} to publish without counting.",
            details={"limit": "live_listings", "plan": plans.PROFESSIONAL},
        )


async def assert_can_add_member(session: AsyncSession, publisher_id: uuid.UUID) -> None:
    """Refuse a seat the plan does not include.

    Pending invitations count. Otherwise a free account invites twenty people,
    none of whom have accepted yet, and the limit binds when they do - refusing
    nineteen of them at the moment they try to join, which is the worst possible
    place to discover it.

    The owner is not counted: they are a column on the publisher rather than a
    membership row, and counting them would make a plan offering five seats
    deliver four.
    """
    entitlements = await entitlements_for(session, publisher_id)
    if entitlements.max_team_members is None:
        return

    taken = (
        await session.execute(
            select(func.count())
            .select_from(PublisherMember)
            .where(
                PublisherMember.publisher_id == publisher_id,
                PublisherMember.status.in_([MEMBERSHIP_ACTIVE, MEMBERSHIP_INVITED]),
            )
        )
    ).scalar_one()

    if taken >= entitlements.max_team_members:
        raise PlanLimitError(
            f"This plan includes {entitlements.max_team_members} "
            f"{'seat' if entitlements.max_team_members == 1 else 'seats'} beside your "
            f"own, and they are taken. A larger plan adds more.",
            details={"limit": "team_members", "plan": plans.PROFESSIONAL},
        )


async def assert_has_ai_assistant(session: AsyncSession, publisher_id: uuid.UUID) -> None:
    entitlements = await entitlements_for(session, publisher_id)
    if not entitlements.ai_assistant:
        raise PlanLimitError(
            "The writing assistant comes with "
            f"{_upgrade_to(plans.PROFESSIONAL)}.",
            details={"limit": "ai_assistant", "plan": plans.PROFESSIONAL},
        )


async def assert_has_api_access(session: AsyncSession, publisher_id: uuid.UUID) -> None:
    entitlements = await entitlements_for(session, publisher_id)
    if not entitlements.api_access:
        raise PlanLimitError(
            f"API keys come with {_upgrade_to(plans.BUSINESS)}.",
            details={"limit": "api_access", "plan": plans.BUSINESS},
        )


async def analytics_window_for(session: AsyncSession, publisher_id: uuid.UUID) -> int:
    """How many days back the analytics screen may look.

    A number rather than a refusal, because the whole screen working and showing
    less is a better answer than the screen refusing - and because the limit is
    what is being sold, not a wall.
    """
    return (await entitlements_for(session, publisher_id)).analytics_window_days


__all__ = [
    "PlanLimitError",
    "analytics_window_for",
    "assert_can_add_member",
    "assert_can_publish_another",
    "assert_has_ai_assistant",
    "assert_has_api_access",
    "entitlements_for",
    "plan_of",
]

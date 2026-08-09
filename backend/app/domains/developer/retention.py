"""Forgetting old webhook deliveries.

The one place in the platform that deletes on a timer, which is worth being
explicit about given how firmly spec BUSINESS-07 says withhold rather than
destroy. That rule protects people's content and their accounts. A delivery row
is neither: it is a copy of a message we already sent to somebody else's server,
kept only so its owner can debug the integration. After a month it answers no
question anybody is asking, and keeping every event the platform ever emitted
would make the table the largest thing in the database.

Delivered rows go first and hardest. A failure is the interesting one - it is
the reason somebody opens this page at all - so failures are kept longer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domains.developer.webhooks import (
    DELIVERY_DELIVERED,
    HISTORY_DAYS,
    WebhookDelivery,
)

logger = get_logger("mado.webhook_retention")

# A successful delivery a week old tells you the integration works, which you
# already knew from the more recent ones above it.
DELIVERED_DAYS = 7


async def forget_old_deliveries(session: AsyncSession) -> int:
    """Remove history nobody can act on any more. Returns how many rows went."""
    now = datetime.now(UTC)

    delivered = await session.execute(
        delete(WebhookDelivery).where(
            WebhookDelivery.status == DELIVERY_DELIVERED,
            WebhookDelivery.created_at < now - timedelta(days=DELIVERED_DAYS),
        )
    )
    everything_else = await session.execute(
        delete(WebhookDelivery).where(
            WebhookDelivery.created_at < now - timedelta(days=HISTORY_DAYS)
        )
    )
    return (delivered.rowcount or 0) + (everything_else.rowcount or 0)

"""withhold high-risk content already published

Revision ID: 77a0681605de
Revises: 7bacd650b168
Create Date: 2026-08-06 13:51:43.178049
"""
from __future__ import annotations

from collections.abc import Sequence

# Custom column types autogenerate can emit. Imported unconditionally because the
# render hooks that would otherwise add them do not fire in every case - notably
# geoalchemy2 columns wrapped in sa.Computed.
import geoalchemy2  # noqa: F401
import pgvector.sqlalchemy  # noqa: F401
import sqlalchemy as sa
from alembic import op


revision: str = '77a0681605de'
down_revision: str | None = '7bacd650b168'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply the corrected screening policy to rows already in the table.

    Screening used to route high-risk submissions to 'pending', which is a
    discoverable status, so they stayed live in the feed, in search and inside
    generated itineraries while nominally awaiting review. New posts now go to
    'flagged'; anything already published under the old behaviour is still sitting
    in discovery and has to be moved too, or the fix only protects future readers.

    Deliberately narrow:
      - only rows the screener itself scored at or above the review threshold
      - never a row a moderator has ruled on (moderation_locked), because a human
        decision outranks this and re-flagging approved content would undo it
    """
    op.execute(
        """
        UPDATE catalog.experiences
        SET moderation_status = 'flagged'
        WHERE moderation_status = 'pending'
          AND risk_score >= 0.7
          AND moderation_locked IS NOT TRUE
        """
    )


def downgrade() -> None:
    # Returns the affected rows to discovery. Correct as an inverse, but note that
    # it re-exposes content the screener considered risky.
    op.execute(
        """
        UPDATE catalog.experiences
        SET moderation_status = 'pending'
        WHERE moderation_status = 'flagged'
          AND risk_score >= 0.7
          AND moderation_locked IS NOT TRUE
        """
    )

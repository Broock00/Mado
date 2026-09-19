"""Add draft lifecycle to itineraries.

Explorers can now save unfinished plans as drafts that survive refreshes
and browsing to "Add to Plan" without becoming kept itineraries.

A ``status`` column distinguishes the two states: ``'kept'`` is an itinerary
the explorer chose to hold onto; ``'draft'`` is a workspace that has not
been promoted yet. Every row created before this migration was the result of
an explicit "Keep this plan" action, so they all backfill as ``'kept'``.

The composite index on ``(user_id, status, created_at)`` lets the kept list
and the draft list each read only their own slice without a full-table scan.

Revision ID: b3f1c2e4d0a8
Revises: c0ded3bfd337
Create Date: 2026-09-18 22:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import geoalchemy2  # noqa: F401
import pgvector.sqlalchemy  # noqa: F401
import sqlalchemy as sa
from alembic import op

revision: str = 'b3f1c2e4d0a8'
down_revision: str | None = 'c0ded3bfd337'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'itineraries',
        sa.Column(
            'status',
            sa.String(length=10),
            nullable=False,
            # All existing rows are kept plans — they were only written by an
            # explicit "Keep this plan" action. Drafts come from the new
            # workspace flow and will always get status='draft' from Python.
            server_default='kept',
        ),
        schema='explorer',
    )
    # Composite index for the two list queries: kept plans and draft workspaces.
    op.create_index(
        'ix_itineraries_user_status',
        'itineraries',
        ['user_id', 'status', 'created_at'],
        unique=False,
        schema='explorer',
    )


def downgrade() -> None:
    op.drop_index('ix_itineraries_user_status', table_name='itineraries', schema='explorer')
    op.drop_column('itineraries', 'status', schema='explorer')

"""track embedding model on user memories

Revision ID: 8963c8f0365d
Revises: f56d0a08ca3e
Create Date: 2026-08-06 08:36:47.698047
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


revision: str = '8963c8f0365d'
down_revision: str | None = 'f56d0a08ca3e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'user_memories',
        sa.Column('embedding_model', sa.String(length=64), nullable=True),
        schema='ai',
    )
    # No backfill here, unlike the catalog table: no memory has ever been written,
    # so there is nothing to tag. Rows without a model are simply not recalled
    # semantically, which is the correct behaviour for an unknown vector space.


def downgrade() -> None:
    op.drop_column('user_memories', 'embedding_model', schema='ai')

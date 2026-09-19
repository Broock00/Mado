"""Add visibility to itineraries so kept plans can be shared by link.

Mirrors collections: private (owner only), unlisted (anyone with the link),
public (same read path for now; no browse rail yet). Drafts stay private.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-09-19 09:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import geoalchemy2  # noqa: F401
import pgvector.sqlalchemy  # noqa: F401
import sqlalchemy as sa

from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "itineraries",
        sa.Column(
            "visibility",
            sa.String(length=16),
            nullable=False,
            server_default="private",
        ),
        schema="explorer",
    )
    op.create_index(
        "ix_itineraries_visibility",
        "itineraries",
        ["visibility"],
        schema="explorer",
    )


def downgrade() -> None:
    op.drop_index("ix_itineraries_visibility", table_name="itineraries", schema="explorer")
    op.drop_column("itineraries", "visibility", schema="explorer")

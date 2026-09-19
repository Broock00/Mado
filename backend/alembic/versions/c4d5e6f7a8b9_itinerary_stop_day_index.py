"""Add day_index to itinerary stops for multi-day trips.

V2 of the planner lets explorers build multi-day stays in the same itinerary
model. Each stop belongs to a day (0-based within the stay). Existing outing
plans backfill as day 0 so behaviour is unchanged.

Travel is computed within a day only — the first stop of each day has zero
travel from the previous day's last stop, because overnight is not a hop.

Revision ID: c4d5e6f7a8b9
Revises: a1b2c3d4e5f6
Create Date: 2026-09-19 07:55:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import geoalchemy2  # noqa: F401
import pgvector.sqlalchemy  # noqa: F401
import sqlalchemy as sa

from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "itinerary_stops",
        sa.Column(
            "day_index",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
        ),
        schema="explorer",
    )
    # Composite unique so position is unique within a day, not only globally —
    # two days can both have a stop at position 0.
    op.drop_constraint(
        "uq_itinerary_stop_position",
        "itinerary_stops",
        schema="explorer",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_itinerary_stop_day_position",
        "itinerary_stops",
        ["itinerary_id", "day_index", "position"],
        schema="explorer",
    )
    op.create_index(
        "ix_itinerary_stops_itinerary_day",
        "itinerary_stops",
        ["itinerary_id", "day_index"],
        schema="explorer",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_itinerary_stops_itinerary_day",
        table_name="itinerary_stops",
        schema="explorer",
    )
    op.drop_constraint(
        "uq_itinerary_stop_day_position",
        "itinerary_stops",
        schema="explorer",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_itinerary_stop_position",
        "itinerary_stops",
        ["itinerary_id", "position"],
        schema="explorer",
    )
    op.drop_column("itinerary_stops", "day_index", schema="explorer")

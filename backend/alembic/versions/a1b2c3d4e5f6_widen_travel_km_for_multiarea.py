"""Widen itinerary_stops.travel_km for multi-area plans.

The builder allows stops in different cities/countries. Haversine between
continents exceeds Numeric(6, 2) (max 9999.99 km) — Addis to Tokyo is ~10402 km
and crashed Add Stop with numeric field overflow. Numeric(8, 2) covers any
Earth distance with headroom.

Revision ID: a1b2c3d4e5f6
Revises: b3f1c2e4d0a8
Create Date: 2026-09-19 07:20:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import geoalchemy2  # noqa: F401
import pgvector.sqlalchemy  # noqa: F401
import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "b3f1c2e4d0a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "itinerary_stops",
        "travel_km",
        existing_type=sa.Numeric(precision=6, scale=2),
        type_=sa.Numeric(precision=8, scale=2),
        existing_nullable=True,
        schema="explorer",
    )


def downgrade() -> None:
    op.alter_column(
        "itinerary_stops",
        "travel_km",
        existing_type=sa.Numeric(precision=8, scale=2),
        type_=sa.Numeric(precision=6, scale=2),
        existing_nullable=True,
        schema="explorer",
    )

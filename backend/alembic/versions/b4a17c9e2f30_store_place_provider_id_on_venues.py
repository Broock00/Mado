"""store the place provider's id on venues

Google's terms permit caching most place data for thirty days and single out
`place_id` as the field that may be kept indefinitely. This column is that
exception and nothing more: it records which place a venue's coordinates came
from, so the same address added twice can be recognised as the same address and
so a disputed pin can be re-resolved.

It is emphatically not the beginning of a places table. Nothing looks a venue up
by it, which is why it is nullable and carries no index - most venues predate it,
and a venue whose publisher dropped a pin on a map has no provider id at all.

Revision ID: b4a17c9e2f30
Revises: 5db5fc089dfa
Create Date: 2026-08-16 00:00:00.000000
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


revision: str = 'b4a17c9e2f30'
down_revision: str | None = '5db5fc089dfa'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'venues',
        # 512 rather than something tighter: Google documents place ids as
        # opaque and variable-length, and explicitly warns against assuming a
        # maximum. A truncated identifier resolves to nothing and looks like a
        # deleted place.
        sa.Column('place_id', sa.String(length=512), nullable=True),
        schema='catalog',
    )


def downgrade() -> None:
    op.drop_column('venues', 'place_id', schema='catalog')

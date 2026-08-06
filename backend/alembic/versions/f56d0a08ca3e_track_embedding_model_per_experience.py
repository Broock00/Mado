"""track embedding model per experience

Revision ID: f56d0a08ca3e
Revises: 42292debc492
Create Date: 2026-08-06 08:16:47.247324
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


revision: str = 'f56d0a08ca3e'
down_revision: str | None = '42292debc492'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'experiences',
        sa.Column('embedding_model', sa.String(length=64), nullable=True),
        schema='catalog',
    )

    # Existing vectors all came from the hashing provider - that was the only one
    # that had ever run against this database. Backfilling the tag rather than
    # leaving it NULL keeps those rows retrievable instead of silently dropping
    # them out of vector search the moment the model filter goes live.
    op.execute(
        "UPDATE catalog.experiences SET embedding_model = 'hashing-v1' "
        "WHERE embedding IS NOT NULL AND embedding_model IS NULL"
    )

    # Partial index supporting the model filter that every vector query now
    # carries. The HNSW index answers "nearest"; this one keeps the equality
    # predicate from degrading into a scan as models accumulate.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_experiences_embedding_model "
        "ON catalog.experiences (embedding_model) WHERE embedding IS NOT NULL"
    )
    # NOTE: autogenerate also proposed dropping ix_experiences_embedding_hnsw. That
    # index is created by raw SQL in 42292debc492 because SQLAlchemy cannot express
    # HNSW operator classes, so autogenerate cannot see it in the model metadata and
    # reports it as removed on every run. Dropping it is never correct here.


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS catalog.ix_experiences_embedding_model")
    op.drop_column('experiences', 'embedding_model', schema='catalog')

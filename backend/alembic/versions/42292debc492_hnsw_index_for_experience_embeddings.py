"""hnsw index for experience embeddings

Revision ID: 42292debc492
Revises: 6d41aa81fdf6
Create Date: 2026-08-06 08:05:23.870971
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


revision: str = '42292debc492'
down_revision: str | None = '6d41aa81fdf6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # HNSW rather than IVFFlat: it needs no training pass, so it works on an empty
    # table and stays correct as rows arrive - which matters here because the
    # catalogue grows continuously from publishing rather than being bulk-loaded.
    #
    # vector_cosine_ops matches the `<=>` operator the query uses. A mismatch here
    # is silent: the index is simply never chosen, and search quietly degrades to
    # a sequential scan.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_experiences_embedding_hnsw
        ON catalog.experiences
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS catalog.ix_experiences_embedding_hnsw")

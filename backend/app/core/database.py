"""Async database engine and session management.

Every domain owns its own Postgres schema (spec 80.04 s5). Models declare that
ownership through ``__table_args__ = {"schema": ...}``; nothing here reaches across
a boundary on a caller's behalf.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core import metrics, tracing
from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug and settings.environment == "development",
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)


# Time every statement into the request's `db` span (spec OPERATIONS-42 §5).
#
# On the engine's cursor events rather than around `session.execute`, because
# that is the only place that also catches the statements SQLAlchemy issues on
# its own behalf - lazy loads, flushes, the pre-ping - which are exactly the
# ones that surprise you. A request whose time is all in `db` when the handler
# looks like two queries has told you it is doing forty.
#
# The statement text is deliberately not recorded. It would be the largest thing
# in every log line, and it can contain literal values that have no business
# being logged.
@event.listens_for(engine.sync_engine, "before_cursor_execute")
def _before_cursor(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
    conn.info["mado_query_started"] = time.perf_counter()


@event.listens_for(engine.sync_engine, "after_cursor_execute")
def _after_cursor(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
    started = conn.info.pop("mado_query_started", None)
    if started is None:
        return
    elapsed = time.perf_counter() - started
    # Accumulated by name, so forty queries become one `db` total rather than
    # forty entries - which is the number an operator acts on.
    tracing.add_span("db", elapsed)
    # Duration only, no call counter. A per-statement counter would be the
    # highest-frequency metric in the process and would answer a question
    # nobody asks; a failing query surfaces as a 5xx in the HTTP metrics and as
    # a stack trace in the log, both of which say more.
    metrics.dependency_duration.observe(elapsed, "db")

SessionFactory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Declarative base shared by every domain's models.

    ``eager_defaults`` makes flush fetch server-generated values in the same
    statement, via Postgres ``RETURNING``. Without it, a column like
    ``updated_at`` (``onupdate=func.now()``) is computed by the database and left
    expired on the instance, so the next attribute read emits a lazy SELECT - which
    raises ``MissingGreenlet`` under an async session, at whatever unrelated line
    happened to touch the attribute first.

    Setting it here rather than per-model means no future timestamped table has to
    rediscover this.
    """

    __mapper_args__ = {"eager_defaults": True}


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session.

    **This dependency deliberately does not commit.** FastAPI runs the exit code of
    a ``yield`` dependency *after the response has been sent*, so committing here
    would let a client act on a 201 before the transaction was durable - a client
    that immediately used a just-issued refresh token could be told it was invalid.

    Handlers therefore own their transaction boundary and commit explicitly before
    returning, which also satisfies spec 80.04 s16's requirement that transactions
    be short and explicit. Rollback on failure stays here so a raised error can
    never leave partial state behind.
    """
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise

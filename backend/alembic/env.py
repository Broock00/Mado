"""Alembic environment.

Runs migrations through the same async engine the application uses, so a migration
cannot pass against a driver the app never touches.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.asyncio_compat import run as run_async
from app.core.config import get_settings
from app.models import Base  # noqa: F401  (registers every domain's tables)

config = context.config
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Schemas the application owns. Anything outside this set (postgis's own tables,
# for instance) must be invisible to autogenerate or every run emits spurious drops.
MADO_SCHEMAS = {"identity", "publisher", "catalog", "explorer", "ai", "commerce"}


# Indexes created by raw SQL because SQLAlchemy's metadata cannot express them:
# HNSW needs a vector operator class, and the model-tag index is partial. They are
# absent from Base.metadata by definition, so autogenerate reflects them from the
# database, finds no counterpart, and proposes dropping them on *every* run. Left
# unhandled that is a trap - one unreviewed migration silently drops the ANN index
# and vector search quietly degrades to a sequential scan.
RAW_SQL_INDEXES = {
    "ix_experiences_embedding_hnsw",
    "ix_experiences_embedding_model",
}


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return obj.schema in MADO_SCHEMAS
    if type_ == "index" and name in RAW_SQL_INDEXES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        include_object=include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    run_async(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

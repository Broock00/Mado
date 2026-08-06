"""AI domain models (spec 54.05).

Mado owns conversation state; model providers stay stateless (spec 82.02 s16).
That is why the full turn history, tool calls and derived memories live here rather
than being reconstructed from a provider's session.

Memory is split by authority, per spec 56.01 s18: an explicit user statement
outranks a behavioural inference, and each memory records which it is.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.mixins import SoftDelete, Timestamps, UUIDPrimaryKey
from app.domains.catalog.models import EMBEDDING_DIM

if TYPE_CHECKING:
    pass

SCHEMA = "ai"

SOURCE_EXPLICIT = "explicit_user_statement"
SOURCE_INFERRED = "behavioural_inference"


class Conversation(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_user_updated", "user_id", "updated_at"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("identity.users.id", ondelete="CASCADE"),
        default=None,
        index=True,
    )
    # Anonymous explorers get a concierge too (spec 10.01.01), keyed by client id.
    anonymous_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    title: Mapped[str | None] = mapped_column(String(200), default=None)
    city_slug: Mapped[str | None] = mapped_column(String(120), default=None)
    # Carries active goals, clarification state and last-known location so a
    # follow-up like "what about tomorrow?" resolves without re-asking.
    state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        {"schema": SCHEMA},
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.conversations.id", ondelete="CASCADE"),
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user|assistant|system
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Execution trace (spec 56.01 s3.7: every AI execution is traceable) ---
    intent: Mapped[str | None] = mapped_column(String(48), default=None)
    intent_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), default=None)
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    prompt_version: Mapped[str | None] = mapped_column(String(32), default=None)
    tool_calls: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Experience/event ids the answer is grounded in, so the client can render
    # result cards and the platform can verify the answer cited real records.
    referenced_entities: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    token_usage: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class UserMemory(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "user_memories"
    __table_args__ = (
        Index("ix_user_memories_user_category", "user_id", "category"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("identity.users.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(32), default="preference", nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), default=None)
    attribute: Mapped[str | None] = mapped_column(String(64), default=None)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0.500, nullable=False)
    source: Mapped[str] = mapped_column(String(48), default=SOURCE_INFERRED, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), default=None)
    # The model that produced `embedding`. Recall compares a query vector against
    # these, and vectors from different models are not comparable - see the same
    # column on catalog.experiences.
    embedding_model: Mapped[str | None] = mapped_column(String(64), default=None)
    # Spec 54.05: memory decays. A stale inference should stop steering results.
    last_reinforced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    @property
    def is_explicit(self) -> bool:
        return self.source == SOURCE_EXPLICIT

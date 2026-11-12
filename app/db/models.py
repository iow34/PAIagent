import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 1536  # for text-embedding-3-small


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class ServiceAPIKey(Base):
    __tablename__ = "service_api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    key_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
    )
    is_active: Mapped[bool] = mapped_column(
        default=True,
        server_default=true(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (Index("ix_documents_tenant_user", "tenant_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"),
    )
    user_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50), default="pending")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    embedding_model: Mapped[str | None] = mapped_column(String(100))
    embedding_dimensions: Mapped[int | None] = mapped_column()
    embedding_tokens: Mapped[int | None] = mapped_column()
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexing_error_code: Mapped[str | None] = mapped_column(String(50))

    indexing_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    indexing_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int] = mapped_column()
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    document: Mapped["Document"] = relationship(back_populates="chunks")


class ConversationSession(Base):
    __tablename__ = "conversation_sessions"

    __table_args__ = (
        Index(
            "ix_conversation_sessions_owner",
            "tenant_id",
            "user_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"),
    )

    user_id: Mapped[str] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "position",
            name="uq_conversation_messages_session_position",
        ),
        UniqueConstraint(
            "session_id",
            "request_id",
            "role",
            name="uq_conversation_messages_session_request_role",
        ),
        CheckConstraint(
            "position >= 0",
            name="ck_conversation_messages_position",
        ),
        CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_conversation_messages_role",
        ),
        CheckConstraint(
            "(role = 'user' AND answer_status IS NULL) OR "
            "(role = 'assistant' AND answer_status IS NOT NULL "
            "AND answer_status IN ('answered', 'insufficient_information'))",
            name="ck_conversation_messages_answer_status",
        ),
        CheckConstraint(
            "jsonb_typeof(sources) = 'array'",
            name="ck_conversation_messages_sources_array",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
    )

    position: Mapped[int] = mapped_column()

    role: Mapped[str] = mapped_column(String(20))

    content: Mapped[str] = mapped_column(Text)

    answer_status: Mapped[str | None] = mapped_column(String(32))

    sources: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class DocumentKnowledgeRecord(Base):
    __tablename__ = "document_knowledge"

    __table_args__ = (
        CheckConstraint(
            "coverage IN ('full', 'partial')",
            name="ck_document_knowledge_coverage",
        ),
        CheckConstraint(
            "length(btrim(summary)) > 0",
            name="ck_document_knowledge_summary_nonempty",
        ),
        CheckConstraint(
            "jsonb_typeof(summary_chunk_ids) = 'array'",
            name="ck_document_knowledge_summary_chunk_ids_array",
        ),
        CheckConstraint(
            "jsonb_typeof(topics) = 'array'",
            name="ck_document_knowledge_topics_array",
        ),
        CheckConstraint(
            "jsonb_typeof(facts) = 'array'",
            name="ck_document_knowledge_facts_array",
        ),
        CheckConstraint(
            "jsonb_typeof(sources) = 'array'",
            name="ck_document_knowledge_sources_array",
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )

    summary: Mapped[str] = mapped_column(Text)

    summary_chunk_ids: Mapped[list[str]] = mapped_column(JSONB)

    topics: Mapped[list[str]] = mapped_column(JSONB)

    facts: Mapped[list[dict[str, object]]] = mapped_column(JSONB)

    sources: Mapped[list[dict[str, object]]] = mapped_column(JSONB)

    coverage: Mapped[str] = mapped_column(String(16))

    generation_model: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

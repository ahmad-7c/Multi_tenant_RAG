import uuid
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Denormalized from documents.org_id rather than joined at query time.
    # Retrieval does a similarity search directly against this table on the
    # hot query path; requiring a join to documents just to enforce the
    # tenant filter would mean every vector search pays for it.
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # NULL = parent chunk, non-null = child chunk pointing at its parent.
    # One self-referential table instead of two near-identical tables.
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=True, index=True
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Only child chunks are embedded and searched; parent chunks are sent to
    # the LLM as context and are never compared by vector distance.
    embedding: Mapped[Optional[list[float]]] = mapped_column(Vector(1536), nullable=True)

    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)

    document: Mapped["Document"] = relationship(back_populates="chunks")
    parent: Mapped[Optional["DocumentChunk"]] = relationship(
        remote_side=[id], back_populates="children"
    )
    children: Mapped[list["DocumentChunk"]] = relationship(back_populates="parent")

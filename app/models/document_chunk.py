import uuid
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.db.base import Base

# Must match migration 0004's to_tsvector() config. Duplicated rather than
# shared because the migration cannot import a value that might change.
TS_CONFIG = "english"


class DocumentChunk(Base):
    """One parent or child chunk.

    Parent and child live in the same table, discriminated by parent_id
    (see below) rather than split across two near-identical tables. The
    alternative -- parent_chunks + child_chunks -- would duplicate every
    column and turn "give me the parents of these children" into a join
    across two tables instead of a self-join on one.
    """

    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Denormalized from documents.org_id rather than joined at query time.
    # Retrieval runs an ANN search directly against this table on the hot
    # query path; if the tenant filter lived on documents, every vector
    # search would have to join before it could filter, and the filter is
    # the one predicate that must never be optional. Keeping it on the row
    # means the WHERE clause and the index are on the same table as the
    # vectors.
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # The parent/child discriminator. NULL means "this row IS a parent",
    # not "this row has no parent yet": parents are the context sent to the
    # LLM, children are the units that get embedded and searched.
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=True, index=True
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Children only. Parents are never compared by vector distance, so a
    # NULL here is the normal state for half the table -- which is also why
    # the HNSW index is partial on `embedding IS NOT NULL`.
    #
    # Width comes from Settings while the migration hardcodes 1536. That is
    # deliberate: a mismatch fails loudly on the first insert ("expected
    # 1536 dimensions") rather than silently writing truncated vectors.
    embedding: Mapped[Optional[list[float]]] = mapped_column(
        Vector(settings.EMBEDDING_DIMENSIONS), nullable=True
    )

    # Which model produced `embedding`. Without it, re-embedding onto a new
    # model means guessing what is already stored; with it, a mixed-model
    # table is at least detectable.
    embedding_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Maintained by Postgres, never by application code. Declared Computed
    # so SQLAlchemy marks it read-only and leaves it out of INSERT/UPDATE
    # column lists -- Postgres rejects writes to a generated column, and
    # more importantly no write path can forget to keep it in sync with
    # content.
    content_tsv: Mapped[Optional[str]] = mapped_column(
        TSVECTOR,
        Computed(f"to_tsvector('{TS_CONFIG}', content)", persisted=True),
        nullable=True,
    )

    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Measured with tiktoken at ingest time and stored, so the prompt
    # builder can pack chunks against CONTEXT_TOKEN_BUDGET without
    # re-tokenizing every candidate on every request.
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)

    document: Mapped["Document"] = relationship(back_populates="chunks")
    parent: Mapped[Optional["DocumentChunk"]] = relationship(
        remote_side=[id], back_populates="children"
    )
    children: Mapped[list["DocumentChunk"]] = relationship(back_populates="parent")

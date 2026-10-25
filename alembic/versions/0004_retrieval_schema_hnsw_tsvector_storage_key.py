"""retrieval schema: storage_key, embedding_model, tsvector+GIN, HNSW

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25

Takes the Phase-2 chunk tables from "ingested" to "searchable": the two
retrieval legs (vector + keyword) each get the index they actually need,
and the document row stops pointing at a host filesystem.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Hardcoded, not read from Settings, on purpose: a migration is a
# historical snapshot of what was applied to a database. If it read
# settings.EMBEDDING_DIMENSIONS, changing that env var would retroactively
# change what this already-applied migration claims to have done. The
# model reads the setting, so a mismatch fails loudly at insert time
# ("expected 1536 dimensions") instead of corrupting anything.
EMBEDDING_DIM = 1536

# Must be a literal too. to_tsvector(regconfig, text) is IMMUTABLE and so
# is legal in a generated column; the one-argument to_tsvector(text) is
# only STABLE because it reads default_text_search_config at runtime, and
# Postgres rejects it here. That also means the search language is baked
# into the schema: supporting a second language is a migration, not a
# setting.
TS_CONFIG = "english"


def upgrade() -> None:
    # Idempotent and repeated from 0002 so this migration can also be the
    # first one applied to a database restored from a pre-vector dump.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ---- documents: a key, not a path ---------------------------------
    # file_path leaked a storage implementation into the schema: it forced
    # the API and the worker onto one shared filesystem and could not
    # describe an object in a bucket. storage_key is opaque -- the storage
    # backend alone knows how to turn it into bytes.
    op.alter_column(
        "documents",
        "file_path",
        new_column_name="storage_key",
        type_=sa.String(1024),
        existing_type=sa.String(512),
        existing_nullable=False,
    )

    # ---- document_chunks: which model produced this vector ------------
    # Without this there is no way to tell a text-embedding-3-small vector
    # from any other, so re-embedding onto a new model becomes a guess
    # about what is already in the table. Nullable because parents are
    # never embedded.
    op.add_column(
        "document_chunks", sa.Column("embedding_model", sa.String(100), nullable=True)
    )

    # ---- keyword search leg -------------------------------------------
    # GENERATED ALWAYS, not a trigger and not application code: the DB
    # becomes the only writer, so content_tsv cannot drift from content.
    # A chunk that vector search can find but keyword search cannot does
    # not raise anything -- it just quietly costs that tenant recall on
    # one of the two legs, and RRF hides the asymmetry.
    op.execute(
        f"ALTER TABLE document_chunks ADD COLUMN content_tsv tsvector "
        f"GENERATED ALWAYS AS (to_tsvector('{TS_CONFIG}', content)) STORED"
    )
    # Partial on children: parents are sent to the LLM, never searched, so
    # indexing them would grow the index for rows no query can match.
    # Postgres can bitmap-AND this with ix_document_chunks_org_id to apply
    # the tenant filter; a composite (org_id, content_tsv) GIN would need
    # the btree_gin extension and is not worth it until measured.
    op.execute(
        "CREATE INDEX ix_document_chunks_content_tsv "
        "ON document_chunks USING gin (content_tsv) "
        "WHERE parent_id IS NOT NULL"
    )

    # ---- vector search leg --------------------------------------------
    # ivfflat out, HNSW in. ivfflat derives its centroids from the rows
    # present when the index is built; 0002 built it on an empty table, so
    # its 100 lists were meaningless until a REINDEX nobody was going to
    # run. HNSW builds incrementally -- a graph that is correct after the
    # first insert and stays correct -- and gives materially better
    # recall-at-speed, which matters because every tenant-filtered query
    # here is already fighting for recall.
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_ivfflat")
    # m / ef_construction are pgvector defaults, stated explicitly so a
    # future tuning change is a visible diff rather than a silent
    # dependency on the library default.
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding_hnsw "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64) "
        "WHERE embedding IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_hnsw")
    op.execute(
        f"CREATE INDEX ix_document_chunks_embedding_ivfflat "
        f"ON document_chunks USING ivfflat (embedding vector_cosine_ops) "
        f"WITH (lists = 100)"
    )
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_content_tsv")
    op.drop_column("document_chunks", "content_tsv")
    op.drop_column("document_chunks", "embedding_model")
    op.alter_column(
        "documents",
        "storage_key",
        new_column_name="file_path",
        type_=sa.String(512),
        existing_type=sa.String(1024),
        existing_nullable=False,
    )

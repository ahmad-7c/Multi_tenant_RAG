"""Schema-level tests.

These assert things that only exist in Postgres: the vector extension, the
index *method* and *operator class*, and the generated tsvector column.
They are the reason the suite cannot run on SQLite -- every one of these
assertions would be vacuously true there.
"""
import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk


def _indexes(db_session, table: str) -> dict[str, str]:
    rows = db_session.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"),
        {"t": table},
    ).all()
    return {name: definition for name, definition in rows}


def test_vector_extension_is_installed(db_session):
    installed = db_session.execute(
        text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
    ).scalar()
    assert installed == 1


def test_embedding_column_has_the_configured_dimension(db_session):
    # atttypmod carries the declared dimension for the pgvector type.
    dim = db_session.execute(
        text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'document_chunks'::regclass AND attname = 'embedding'"
        )
    ).scalar()
    assert dim == settings.EMBEDDING_DIMENSIONS


def test_embedding_index_is_hnsw_with_cosine_ops(db_session):
    definitions = _indexes(db_session, "document_chunks")
    hnsw = [d for d in definitions.values() if "USING hnsw" in d]

    assert hnsw, f"no HNSW index on document_chunks; got {list(definitions)}"
    # The operator class must match the distance operator used at query
    # time. An index built with l2 ops is never used by a cosine query --
    # it degrades silently to a sequential scan, which still returns
    # correct-looking results, just slowly.
    assert "vector_cosine_ops" in hnsw[0]


def test_ivfflat_index_is_gone(db_session):
    definitions = _indexes(db_session, "document_chunks")
    assert not [d for d in definitions.values() if "ivfflat" in d]


def test_content_tsv_has_a_gin_index_limited_to_children(db_session):
    definitions = _indexes(db_session, "document_chunks")
    gin = [d for d in definitions.values() if "USING gin" in d]

    assert gin, f"no GIN index on document_chunks; got {list(definitions)}"
    assert "content_tsv" in gin[0]
    # Partial: only children are ever keyword-searched, so indexing parents
    # would grow the index for rows no query can match.
    assert "parent_id IS NOT NULL" in gin[0]


def test_documents_has_storage_key_and_not_file_path(db_session):
    columns = {
        r[0]
        for r in db_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'documents'"
            )
        ).all()
    }
    assert "storage_key" in columns
    # file_path implied a host filesystem path, which cannot survive a move
    # to object storage.
    assert "file_path" not in columns


def test_organizations_index_version_defaults_to_zero(db_session, org_factory):
    org = org_factory()
    db_session.refresh(org)
    assert org.index_version == 0


@pytest.fixture()
def document(db_session, org_factory, user_factory):
    org = org_factory()
    admin = user_factory(org)
    doc = Document(
        id=uuid.uuid4(),
        org_id=org.id,
        uploaded_by=admin.id,
        filename="handbook.pdf",
        storage_key=f"{org.id}/{uuid.uuid4()}/handbook.pdf",
        file_size_bytes=42,
        status=DocumentStatus.PENDING,
    )
    db_session.add(doc)
    db_session.commit()
    return doc


def test_content_tsv_is_generated_by_the_database(db_session, document):
    """Nothing in application code writes content_tsv.

    The column is GENERATED ALWAYS, so the only way for it to be wrong is
    for `content` to be wrong -- a write path cannot forget to maintain it.
    """
    chunk = DocumentChunk(
        id=uuid.uuid4(),
        org_id=document.org_id,
        document_id=document.id,
        parent_id=None,
        content="The quarterly revenue forecast was revised upward.",
        embedding=None,
        embedding_model=None,
        page_number=1,
        token_count=9,
        chunk_index=0,
    )
    db_session.add(chunk)
    db_session.commit()

    tsv = db_session.execute(
        text("SELECT content_tsv::text FROM document_chunks WHERE id = :i"), {"i": chunk.id}
    ).scalar()

    # Stemmed by the english config ("revised" -> "revis"), which proves
    # the value really went through to_tsvector and is not a raw copy.
    assert "revis" in tsv
    assert "forecast" in tsv


def test_content_tsv_follows_an_update_to_content(db_session, document):
    chunk = DocumentChunk(
        id=uuid.uuid4(),
        org_id=document.org_id,
        document_id=document.id,
        content="original wording",
        page_number=1,
        token_count=2,
        chunk_index=0,
    )
    db_session.add(chunk)
    db_session.commit()

    db_session.execute(
        text("UPDATE document_chunks SET content = :c WHERE id = :i"),
        {"c": "replacement phrasing", "i": chunk.id},
    )
    db_session.commit()

    tsv = db_session.execute(
        text("SELECT content_tsv::text FROM document_chunks WHERE id = :i"), {"i": chunk.id}
    ).scalar()
    assert "replac" in tsv
    assert "origin" not in tsv


def test_content_tsv_is_mapped_read_only(db_session):
    # If it were a plain mapped column the ORM would INSERT its own value
    # alongside content, and Postgres rejects writes to a generated column.
    column = DocumentChunk.__table__.columns["content_tsv"]
    assert column.computed is not None
    assert column.computed.persisted is True


def test_parent_null_parent_id_child_non_null(db_session, document):
    parent = DocumentChunk(
        id=uuid.uuid4(),
        org_id=document.org_id,
        document_id=document.id,
        parent_id=None,
        content="parent text",
        embedding=None,
        embedding_model=None,
        page_number=1,
        token_count=2,
        chunk_index=0,
    )
    db_session.add(parent)
    db_session.flush()

    child = DocumentChunk(
        id=uuid.uuid4(),
        org_id=document.org_id,
        document_id=document.id,
        parent_id=parent.id,
        content="child text",
        embedding=[0.1] * settings.EMBEDDING_DIMENSIONS,
        embedding_model=settings.EMBEDDING_MODEL,
        page_number=1,
        token_count=2,
        chunk_index=0,
    )
    db_session.add(child)
    db_session.commit()

    # NULL parent_id is the discriminator: it means "this row IS a parent",
    # not "this row has no parent yet".
    assert parent.parent_id is None
    assert child.parent_id == parent.id
    assert parent.embedding is None
    assert child.embedding is not None
    assert child.embedding_model == settings.EMBEDDING_MODEL


def test_deleting_a_document_cascades_to_its_chunks(db_session, document):
    # Captured before the DELETE: commit() expires the ORM instance, and
    # reading document.id afterwards would try to refresh a row that no
    # longer exists.
    document_id = document.id
    parent = DocumentChunk(
        id=uuid.uuid4(),
        org_id=document.org_id,
        document_id=document.id,
        content="p",
        page_number=1,
        token_count=1,
        chunk_index=0,
    )
    db_session.add(parent)
    db_session.flush()
    db_session.add(
        DocumentChunk(
            id=uuid.uuid4(),
            org_id=document.org_id,
            document_id=document.id,
            parent_id=parent.id,
            content="c",
            page_number=1,
            token_count=1,
            chunk_index=0,
        )
    )
    db_session.commit()

    db_session.execute(text("DELETE FROM documents WHERE id = :i"), {"i": document_id})
    db_session.commit()

    # ON DELETE CASCADE on document_chunks.document_id, enforced by the DB
    # rather than by the ORM relationship: a raw DELETE (a backfill script,
    # psql, a bulk purge) must not be able to leave orphaned chunks that
    # retrieval would still happily return.
    remaining = db_session.execute(
        text("SELECT count(*) FROM document_chunks WHERE document_id = :i"),
        {"i": document_id},
    ).scalar()
    assert remaining == 0

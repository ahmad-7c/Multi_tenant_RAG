"""The embedding-model consistency guard.

Why this exists at all: a vector only means something relative to the
model that produced it. Two models embed "annual leave policy" into two
unrelated regions of R^1536 -- not "similar but different", *unrelated*,
the way two different hash functions produce unrelated digests of the
same string. Cosine distance still returns a number for every pair, so
nothing errors. The corpus simply stops being searchable: a query
embedded with model B scores the model-A chunks as noise, retrieval
returns the arithmetically-least-bad garbage, and the confidence gate --
which is calibrated against a reranker, not against the embedder -- has
no way to tell that the whole candidate set is meaningless.

That failure is silent, degrades answers rather than breaking them, and
looks exactly like "the RAG just isn't very good". So the system refuses
to serve queries instead.
"""
import uuid

import pytest
from sqlalchemy import text as sql_text

from app.services.embedding import (
    EmbeddingModelMismatch,
    check_embedding_model_consistency,
)


def _insert_chunk(db, *, org_id, document_id, model, dimensions=1536):
    """One child chunk carrying a specific embedding_model label."""
    chunk_id = uuid.uuid4()
    db.execute(
        sql_text(
            """
            INSERT INTO document_chunks
                (id, org_id, document_id, parent_id, content, embedding,
                 embedding_model, page_number, token_count, chunk_index)
            VALUES
                (:id, :org_id, :document_id, NULL, :content,
                 CAST(:embedding AS vector), :model, 1, 3, 0)
            """
        ),
        {
            "id": chunk_id,
            "org_id": org_id,
            "document_id": document_id,
            "content": "a chunk",
            "embedding": "[" + ",".join(["0.1"] * dimensions) + "]",
            "model": model,
        },
    )
    return chunk_id


@pytest.fixture()
def document(db_session, org_factory, user_factory):
    from app.models.document import Document, DocumentStatus

    org = org_factory()
    user = user_factory(org=org)
    doc = Document(
        id=uuid.uuid4(),
        org_id=org.id,
        uploaded_by=user.id,
        filename="policy.pdf",
        storage_key="x.pdf",
        file_size_bytes=10,
        status=DocumentStatus.READY,
    )
    db_session.add(doc)
    db_session.flush()
    return doc


def test_guard_passes_on_an_empty_corpus(db_session):
    # A brand new deployment has nothing stored, so no model can conflict.
    check_embedding_model_consistency(db_session, configured_model="gemini-embedding-001")


def test_guard_passes_when_every_chunk_matches_the_configured_model(db_session, document):
    _insert_chunk(
        db_session,
        org_id=document.org_id,
        document_id=document.id,
        model="gemini-embedding-001",
    )

    check_embedding_model_consistency(db_session, configured_model="gemini-embedding-001")


def test_guard_refuses_when_a_chunk_came_from_a_different_model(db_session, document):
    _insert_chunk(
        db_session,
        org_id=document.org_id,
        document_id=document.id,
        model="text-embedding-3-small",
    )

    with pytest.raises(EmbeddingModelMismatch) as excinfo:
        check_embedding_model_consistency(
            db_session, configured_model="gemini-embedding-001"
        )

    message = str(excinfo.value)
    # The message has to name both models and say what to do, because the
    # operator seeing it has no other signal that anything is wrong.
    assert "text-embedding-3-small" in message
    assert "gemini-embedding-001" in message
    assert "re-ingest" in message.lower()


def test_guard_reports_every_stale_model_not_just_the_first(db_session, document):
    _insert_chunk(
        db_session,
        org_id=document.org_id,
        document_id=document.id,
        model="text-embedding-3-small",
    )
    _insert_chunk(
        db_session,
        org_id=document.org_id,
        document_id=document.id,
        model="voyage-3",
    )

    with pytest.raises(EmbeddingModelMismatch) as excinfo:
        check_embedding_model_consistency(
            db_session, configured_model="gemini-embedding-001"
        )

    assert "text-embedding-3-small" in str(excinfo.value)
    assert "voyage-3" in str(excinfo.value)


def test_guard_ignores_parent_chunks_which_are_never_embedded(db_session, document):
    # Parents are stored with embedding=NULL and embedding_model=NULL by
    # design. Counting them as a mismatch would make the guard fire on
    # every healthy corpus.
    db_session.execute(
        sql_text(
            """
            INSERT INTO document_chunks
                (id, org_id, document_id, parent_id, content, embedding,
                 embedding_model, page_number, token_count, chunk_index)
            VALUES
                (:id, :org_id, :document_id, NULL, 'parent text', NULL,
                 NULL, 1, 3, 0)
            """
        ),
        {"id": uuid.uuid4(), "org_id": document.org_id, "document_id": document.id},
    )

    check_embedding_model_consistency(db_session, configured_model="gemini-embedding-001")


def test_guard_crosses_org_boundaries_on_purpose(db_session, org_factory, user_factory):
    """The guard is deliberately NOT org-scoped.

    Every other query in this system filters by org_id. This one must not:
    it is an operator-level deployment check, and a stale corpus belonging
    to org B is still a broken deployment when org A asks a question. A
    per-org guard would let one tenant silently serve garbage.
    """
    from app.models.document import Document, DocumentStatus

    org_a, org_b = org_factory(), org_factory()
    docs = []
    for org in (org_a, org_b):
        user = user_factory(org=org)
        doc = Document(
            id=uuid.uuid4(),
            org_id=org.id,
            uploaded_by=user.id,
            filename="f.pdf",
            storage_key="k.pdf",
            file_size_bytes=10,
            status=DocumentStatus.READY,
        )
        db_session.add(doc)
        docs.append(doc)
    db_session.flush()

    _insert_chunk(
        db_session, org_id=org_a.id, document_id=docs[0].id, model="gemini-embedding-001"
    )
    _insert_chunk(
        db_session, org_id=org_b.id, document_id=docs[1].id, model="text-embedding-3-small"
    )

    with pytest.raises(EmbeddingModelMismatch):
        check_embedding_model_consistency(
            db_session, configured_model="gemini-embedding-001"
        )


def test_query_endpoint_refuses_when_the_guard_tripped(
    client, db_session, org_factory, user_factory, auth_headers
):
    """A tripped guard must block queries without blocking everything else.

    Uploads, auth and document management do not depend on vector
    comparability, so they stay up; only the endpoint that would serve a
    silently-wrong answer refuses.
    """
    import uuid as _uuid

    from app.models.conversation import Conversation

    org = org_factory()
    user = user_factory(org)
    conversation = Conversation(
        id=_uuid.uuid4(), org_id=org.id, user_id=user.id, title="t"
    )
    db_session.add(conversation)
    db_session.commit()

    client.app.state.embedding_model_error = "stored chunks used text-embedding-3-small"
    try:
        response = client.post(
            f"/api/v1/conversations/{conversation.id}/messages",
            json={"content": "anything"},
            headers=auth_headers(user),
        )
    finally:
        client.app.state.embedding_model_error = None

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "embedding model" in detail
    # The user-facing message names no model and no config key: that is
    # operator detail, and it goes to the log, not to a tenant's screen.
    assert "text-embedding-3-small" not in detail


def test_the_startup_guard_reads_the_TEST_database(
    db_session, document, monkeypatch, tmp_path
):
    """The lifespan must see the suite's data, not a developer's.

    `app/main.py` opens its own session (`db = SessionLocal()`) for the
    startup checks, which the `get_db` dependency override does not
    cover. For a while that session pointed at the DEV database, so the
    suite's startup state was whatever happened to be in a local
    Postgres -- and running the eval harness with
    EMBEDDING_PROVIDER=fake (which writes chunks tagged
    `fake-embedding`) turned four SSE tests and the /health test red
    without touching a line of the code they exercise.

    This test builds its own TestClient, so it runs the lifespan AFTER
    inserting a mismatched chunk into the test database. If the guard
    read any other database it would find nothing and the assertion
    below would fail.
    """
    from fastapi.testclient import TestClient

    import app.main as main_module
    from app.core.config import settings
    from app.main import app as fastapi_app
    from app.api.deps import get_db

    _insert_chunk(
        db_session,
        org_id=document.org_id,
        document_id=document.id,
        model="a-model-that-is-not-configured",
    )
    db_session.flush()

    class _SuiteSession:
        def __getattr__(self, name):
            return getattr(db_session, name)

        def close(self):
            return None

    monkeypatch.setattr(main_module, "SessionLocal", lambda: _SuiteSession())
    fastapi_app.dependency_overrides[get_db] = lambda: iter([db_session])
    try:
        with TestClient(fastapi_app) as fresh:
            body = fresh.get("/health").json()
    finally:
        fastapi_app.dependency_overrides.clear()

    assert body["status"] == "ok"
    assert "embedding_model_mismatch" in body.get("degraded", [])


def test_other_endpoints_still_work_when_the_guard_tripped(
    client, org_factory, user_factory, auth_headers
):
    org = org_factory()
    user = user_factory(org)

    client.app.state.embedding_model_error = "stale corpus"
    try:
        response = client.get("/api/v1/documents", headers=auth_headers(user))
    finally:
        client.app.state.embedding_model_error = None

    assert response.status_code == 200

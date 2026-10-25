"""Retrieval tests.

Everything here runs against real Postgres + pgvector, because every claim
being made is about the database: the HNSW operator class, iterative index
scans, `ts_rank`, and the interaction between an ANN index and a mandatory
tenant filter. None of it is observable on SQLite.
"""
import uuid

import pytest

from app.core.config import settings
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.services.embedding import FakeEmbeddings
from app.services.retrieval import reciprocal_rank_fusion, retrieve_candidates


@pytest.fixture()
def embedder():
    return FakeEmbeddings(dimensions=settings.EMBEDDING_DIMENSIONS)


def _seed(db_session, org, user, texts, *, embedder, filename="doc.md"):
    """Create one document with one parent and `texts` as its children."""
    document = Document(
        id=uuid.uuid4(),
        org_id=org.id,
        uploaded_by=user.id,
        filename=filename,
        storage_key=f"{org.id}/{uuid.uuid4()}/{filename}",
        file_size_bytes=1,
        status=DocumentStatus.READY,
    )
    db_session.add(document)
    db_session.flush()

    parent = DocumentChunk(
        id=uuid.uuid4(),
        org_id=org.id,
        document_id=document.id,
        parent_id=None,
        content="\n\n".join(texts),
        embedding=None,
        embedding_model=None,
        page_number=1,
        token_count=10,
        chunk_index=0,
    )
    db_session.add(parent)
    db_session.flush()

    children = []
    for index, text_value in enumerate(texts):
        [vector] = embedder.embed([text_value])
        child = DocumentChunk(
            id=uuid.uuid4(),
            org_id=org.id,
            document_id=document.id,
            parent_id=parent.id,
            content=text_value,
            embedding=vector,
            embedding_model=embedder.model,
            page_number=1,
            token_count=5,
            chunk_index=index,
        )
        children.append(child)
    db_session.add_all(children)
    db_session.commit()
    return document, parent, children


# ------------------------------------------------------------------- RRF ----


def test_rrf_scores_by_rank_not_by_score():
    """Rank, not score, is what gets fused.

    The vector leg produces a cosine similarity in [0, 1]; the full-text
    leg produces a ts_rank that is unbounded and depends on term frequency
    and document length. Those numbers are not comparable, and ts_rank has
    no ceiling to normalize against -- so any weighted sum needs a
    per-corpus fudge factor that every ingestion silently re-tunes.
    """
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    merged = reciprocal_rank_fusion([[a, b], [b, c]], k=60)

    # b is rank 2 in the first list and rank 1 in the second, so it beats
    # a (rank 1 in one list, absent from the other).
    assert [chunk_id for chunk_id, _ in merged][0] == b


def test_rrf_uses_the_configured_k():
    a, b = uuid.uuid4(), uuid.uuid4()

    small_k = dict(reciprocal_rank_fusion([[a, b]], k=1))
    large_k = dict(reciprocal_rank_fusion([[a, b]], k=1000))

    # Small k makes rank 1 dominate; large k flattens the curve. Both must
    # still rank a above b.
    assert small_k[a] / small_k[b] > large_k[a] / large_k[b]


def test_rrf_rewards_appearing_in_both_lists():
    both, vector_only = uuid.uuid4(), uuid.uuid4()

    merged = dict(
        reciprocal_rank_fusion([[vector_only, both], [both]], k=60)
    )

    # Agreement between two independent retrievers is the signal hybrid
    # search exists to capture.
    assert merged[both] > merged[vector_only]


def test_rrf_handles_an_empty_leg():
    a = uuid.uuid4()
    assert [cid for cid, _ in reciprocal_rank_fusion([[a], []], k=60)] == [a]


def test_rrf_handles_all_legs_empty():
    assert reciprocal_rank_fusion([[], []], k=60) == []


def test_rrf_is_deterministic_for_ties():
    a, b = uuid.uuid4(), uuid.uuid4()
    first = reciprocal_rank_fusion([[a], [b]], k=60)
    second = reciprocal_rank_fusion([[a], [b]], k=60)
    # Equal scores must not reorder between calls, or pagination and cache
    # keys become unstable.
    assert first == second


# -------------------------------------------------------- the two legs ----


def test_vector_leg_finds_a_semantically_identical_chunk(
    db_session, org_factory, user_factory, embedder
):
    org = org_factory()
    user = user_factory(org)
    _, _, children = _seed(
        db_session,
        org,
        user,
        [
            "Receipts must be submitted within thirty days of travel.",
            "The office kitchen is restocked every Monday morning.",
            "Parking permits are issued by the facilities team.",
        ],
        embedder=embedder,
    )

    [query_vector] = embedder.embed(
        ["Receipts must be submitted within thirty days of travel."]
    )
    results = retrieve_candidates(
        db_session, org_id=org.id, query_text="unrelated words", query_embedding=query_vector
    )

    ids = [r.chunk_id for r in results]
    assert children[0].id in ids
    # An exact-text match must be rank 1 on the vector leg.
    assert ids[0] == children[0].id


def test_full_text_leg_finds_a_keyword_match(
    db_session, org_factory, user_factory, embedder
):
    org = org_factory()
    user = user_factory(org)
    _, _, children = _seed(
        db_session,
        org,
        user,
        [
            "The reimbursement deadline is thirty days.",
            "Zygomatic defenestration of the quux subsystem.",
        ],
        embedder=embedder,
    )

    # A vector that matches nothing, so only the keyword leg can find it.
    [noise] = embedder.embed(["completely unrelated noise vector seed"])
    results = retrieve_candidates(
        db_session,
        org_id=org.id,
        query_text="zygomatic defenestration quux",
        query_embedding=noise,
    )

    assert children[1].id in [r.chunk_id for r in results]


def test_hybrid_beats_either_leg_alone(db_session, org_factory, user_factory, embedder):
    org = org_factory()
    user = user_factory(org)
    texts = [
        "Receipts must be submitted within thirty days of travel.",
        "Expense claims require a receipt and a manager approval.",
        "The quarterly audit covers all expense submissions.",
    ]
    _, _, children = _seed(db_session, org, user, texts, embedder=embedder)

    [exact] = embedder.embed([texts[1]])
    results = retrieve_candidates(
        db_session,
        org_id=org.id,
        query_text="receipts thirty days travel",
        query_embedding=exact,
    )
    ids = [r.chunk_id for r in results]

    # Both the vector-exact chunk and the keyword-exact chunk surface.
    assert children[1].id in ids
    assert children[0].id in ids


def test_results_carry_the_fields_the_prompt_builder_needs(
    db_session, org_factory, user_factory, embedder
):
    org = org_factory()
    user = user_factory(org)
    document, parent, children = _seed(
        db_session, org, user, ["Receipts within thirty days."], embedder=embedder,
        filename="handbook.pdf",
    )

    [vector] = embedder.embed(["Receipts within thirty days."])
    [result] = retrieve_candidates(
        db_session, org_id=org.id, query_text="receipts", query_embedding=vector
    )

    # Everything a citation needs, fetched in the same query rather than
    # N+1'd later.
    assert result.chunk_id == children[0].id
    assert result.parent_id == parent.id
    assert result.document_id == document.id
    assert result.filename == "handbook.pdf"
    assert result.page_number == 1
    assert result.content


# ---------------------------------------------------------- org scoping ----


def test_retrieval_never_returns_another_orgs_chunk(
    db_session, org_factory, user_factory, embedder
):
    org_a, org_b = org_factory(), org_factory()
    user_a, user_b = user_factory(org_a), user_factory(org_b)

    shared_text = "Receipts must be submitted within thirty days of travel."
    _, _, a_children = _seed(db_session, org_a, user_a, [shared_text], embedder=embedder)
    _, _, b_children = _seed(db_session, org_b, user_b, [shared_text], embedder=embedder)

    [vector] = embedder.embed([shared_text])
    results = retrieve_candidates(
        db_session, org_id=org_a.id, query_text="receipts thirty days", query_embedding=vector
    )
    ids = {r.chunk_id for r in results}

    # Byte-identical content in two orgs: the ONLY thing separating them is
    # the org_id filter, on both legs.
    assert a_children[0].id in ids
    assert b_children[0].id not in ids


def test_parents_are_never_returned_as_candidates(
    db_session, org_factory, user_factory, embedder
):
    org = org_factory()
    user = user_factory(org)
    _, parent, _ = _seed(
        db_session, org, user, ["Receipts within thirty days of travel."], embedder=embedder
    )

    [vector] = embedder.embed(["Receipts within thirty days of travel."])
    results = retrieve_candidates(
        db_session, org_id=org.id, query_text="receipts travel", query_embedding=vector
    )

    # Parents have no embedding and are excluded from the GIN index; they
    # are context, not search targets.
    assert parent.id not in {r.chunk_id for r in results}


def test_empty_corpus_returns_no_candidates(db_session, org_factory, embedder):
    org = org_factory()
    [vector] = embedder.embed(["anything"])

    assert (
        retrieve_candidates(
            db_session, org_id=org.id, query_text="anything", query_embedding=vector
        )
        == []
    )


def test_candidate_cap_is_respected(db_session, org_factory, user_factory, embedder, monkeypatch):
    org = org_factory()
    user = user_factory(org)
    texts = [f"Policy clause number {i} about expenses and travel." for i in range(40)]
    _seed(db_session, org, user, texts, embedder=embedder)
    monkeypatch.setattr(settings, "RETRIEVE_CANDIDATES", 7)

    [vector] = embedder.embed([texts[0]])
    results = retrieve_candidates(
        db_session, org_id=org.id, query_text="expenses travel policy", query_embedding=vector
    )

    assert len(results) == 7


def test_no_duplicate_chunks_in_the_merged_result(
    db_session, org_factory, user_factory, embedder
):
    org = org_factory()
    user = user_factory(org)
    text_value = "Receipts must be submitted within thirty days of travel."
    _seed(db_session, org, user, [text_value], embedder=embedder)

    [vector] = embedder.embed([text_value])
    results = retrieve_candidates(
        db_session, org_id=org.id, query_text="receipts thirty days travel", query_embedding=vector
    )

    # A chunk found by BOTH legs must appear once, with its ranks fused --
    # not twice, which would let one chunk occupy two context slots.
    ids = [r.chunk_id for r in results]
    assert len(ids) == len(set(ids))


# ------------------------------------- the ANN + filter recall problem ----


def test_small_tenant_still_gets_a_full_top_k_beside_a_large_tenant(
    db_session, org_factory, user_factory, embedder
):
    """The headline risk of this phase.

    HNSW walks the graph over the WHOLE table and the tenant filter is
    applied to whatever that walk produced. Without iterative index scans,
    a small tenant surrounded by a much larger one asks for N candidates
    and gets a handful -- no error, no warning, just a quietly short result
    set and worse answers for the smallest customers.
    """
    small_org, big_org = org_factory(), org_factory()
    small_user, big_user = user_factory(small_org), user_factory(big_org)

    # The big tenant dominates the table.
    big_texts = [f"Operational note {i} covering logistics and scheduling." for i in range(400)]
    _seed(db_session, big_org, big_user, big_texts, embedder=embedder)

    small_texts = [f"Expense clause {i} on receipts and reimbursement." for i in range(12)]
    _, _, small_children = _seed(
        db_session, small_org, small_user, small_texts, embedder=embedder
    )

    [vector] = embedder.embed([small_texts[0]])
    results = retrieve_candidates(
        db_session,
        org_id=small_org.id,
        query_text="receipts reimbursement expense",
        query_embedding=vector,
    )

    ids = {r.chunk_id for r in results}
    # All twelve of the small tenant's chunks must be reachable, even
    # though 400 of another tenant's rows sit between them in the graph.
    assert len(ids) == 12, f"small tenant got {len(ids)} of 12 candidates"
    assert ids == {c.id for c in small_children}


def test_iterative_scan_settings_are_applied_per_transaction(db_session):
    from sqlalchemy import text

    from app.services.retrieval import apply_search_settings

    apply_search_settings(db_session)

    assert (
        db_session.execute(text("SHOW hnsw.iterative_scan")).scalar()
        == settings.HNSW_ITERATIVE_SCAN
    )
    assert int(db_session.execute(text("SHOW hnsw.ef_search")).scalar()) == (
        settings.HNSW_EF_SEARCH
    )


def test_search_settings_use_set_local_not_set(db_session):
    """SET LOCAL, not SET.

    These are connection-level GUCs on a POOLED connection. A plain SET
    would persist after the request returns and silently change the search
    behaviour of whatever unrelated request got that connection next.
    """
    from sqlalchemy import text

    from app.services.retrieval import SEARCH_SETTINGS_SQL

    for statement in SEARCH_SETTINGS_SQL:
        assert statement.strip().upper().startswith("SET LOCAL ")
    # And it really is transaction-scoped: after a rollback the setting is
    # back to the server default.
    from app.services.retrieval import apply_search_settings

    apply_search_settings(db_session)
    db_session.rollback()
    assert db_session.execute(text("SHOW hnsw.iterative_scan")).scalar() == "off"


# --------------------------------------------------------- query safety ----


@pytest.mark.parametrize(
    "hostile",
    [
        "receipts & | ! ( )",
        "'; DROP TABLE document_chunks; --",
        "<>!@#$%^&*()",
        "   ",
        "AND OR NOT",
    ],
)
def test_hostile_query_text_does_not_error(
    db_session, org_factory, user_factory, embedder, hostile
):
    org = org_factory()
    user = user_factory(org)
    _seed(db_session, org, user, ["Receipts within thirty days."], embedder=embedder)

    [vector] = embedder.embed(["anything"])
    # websearch_to_tsquery never raises on malformed input, unlike
    # to_tsquery/plainto_tsquery -- which is exactly why it is used here:
    # the query string is end-user text.
    retrieve_candidates(
        db_session, org_id=org.id, query_text=hostile, query_embedding=vector
    )


def test_table_still_exists_after_an_injection_attempt(
    db_session, org_factory, user_factory, embedder
):
    from sqlalchemy import text

    org = org_factory()
    user = user_factory(org)
    _seed(db_session, org, user, ["Receipts within thirty days."], embedder=embedder)

    [vector] = embedder.embed(["anything"])
    retrieve_candidates(
        db_session,
        org_id=org.id,
        query_text="'; DROP TABLE document_chunks; --",
        query_embedding=vector,
    )

    assert db_session.execute(text("SELECT count(*) FROM document_chunks")).scalar() >= 1

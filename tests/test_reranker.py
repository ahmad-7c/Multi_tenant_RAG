"""Reranker and confidence-gate tests."""
import asyncio
import uuid

import pytest

from app.core.config import settings
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.services.reranker import (
    NOT_FOUND_ANSWER,
    FakeReranker,
    HostedReranker,
    PassthroughReranker,
    RankedCandidate,
    map_to_parents,
    rerank_with_fallback,
    should_answer,
)
from app.services.retrieval import Candidate


def _candidate(content: str, *, rrf: float = 0.016, parent_id=None, chunk_id=None):
    return Candidate(
        chunk_id=chunk_id or uuid.uuid4(),
        parent_id=parent_id or uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="doc.pdf",
        page_number=1,
        content=content,
        token_count=len(content.split()),
        rrf_score=rrf,
        vector_rank=1,
        text_rank=None,
    )


# ------------------------------------------------------------ passthrough ----


async def test_passthrough_preserves_rrf_order():
    candidates = [_candidate(f"chunk {i}", rrf=0.02 - i * 0.001) for i in range(5)]

    ranked = await PassthroughReranker().rerank("anything", candidates, 5)

    assert [r.candidate.content for r in ranked] == [c.content for c in candidates]
    assert [r.rank for r in ranked] == [1, 2, 3, 4, 5]


async def test_passthrough_respects_top_n():
    candidates = [_candidate(f"chunk {i}") for i in range(10)]

    ranked = await PassthroughReranker().rerank("anything", candidates, 3)

    assert len(ranked) == 3


def test_passthrough_scores_are_not_calibrated():
    # RRF scores are rank reciprocals, not relevance. Comparing them to
    # CONFIDENCE_THRESHOLD would gate on an arithmetic accident.
    assert PassthroughReranker().scores_are_calibrated is False


# ------------------------------------------------------------------- fake ----


async def test_fake_reranker_actually_reorders():
    candidates = [
        _candidate("the office kitchen is restocked on mondays"),
        _candidate("receipts must be submitted within thirty days"),
    ]

    ranked = await FakeReranker().rerank("receipts submitted thirty days", candidates, 2)

    # A fake that merely echoed input order could not distinguish
    # "reranked" from "did nothing", so it scores on BOTH inputs.
    assert "receipts" in ranked[0].candidate.content


async def test_fake_reranker_is_deterministic():
    candidates = [_candidate("alpha beta"), _candidate("gamma delta")]

    first = await FakeReranker().rerank("alpha", candidates, 2)
    second = await FakeReranker().rerank("alpha", candidates, 2)

    assert [r.candidate.chunk_id for r in first] == [r.candidate.chunk_id for r in second]


async def test_fake_reranker_assigns_sequential_ranks():
    candidates = [_candidate(f"text {i}") for i in range(4)]
    ranked = await FakeReranker().rerank("text", candidates, 4)
    assert [r.rank for r in ranked] == [1, 2, 3, 4]


# --------------------------------------------------------------- fallback ----


async def test_timeout_falls_back_to_rrf_order():
    candidates = [_candidate(f"chunk {i}", rrf=0.02 - i * 0.001) for i in range(4)]
    slow = FakeReranker(delay=1.0)

    ranked, used_fallback = await rerank_with_fallback(
        slow, "query", candidates, top_n=4, timeout=0.05
    )

    # A provider outage must not take the product down: RRF order is
    # already a reasonable hybrid ranking.
    assert used_fallback is True
    assert [r.candidate.content for r in ranked] == [c.content for c in candidates]


async def test_error_falls_back_to_rrf_order():
    candidates = [_candidate("chunk a"), _candidate("chunk b")]

    ranked, used_fallback = await rerank_with_fallback(
        FakeReranker(fail=True), "query", candidates, top_n=2
    )

    assert used_fallback is True
    assert len(ranked) == 2


async def test_successful_rerank_does_not_report_fallback():
    candidates = [_candidate("receipts thirty days"), _candidate("kitchen mondays")]

    ranked, used_fallback = await rerank_with_fallback(
        FakeReranker(), "receipts", candidates, top_n=2
    )

    assert used_fallback is False
    assert ranked


async def test_fallback_on_empty_candidates_is_not_a_failure():
    ranked, used_fallback = await rerank_with_fallback(FakeReranker(fail=True), "q", [])
    assert ranked == []
    assert used_fallback is False


async def test_reranker_does_not_block_the_event_loop():
    """The call must be awaitable, not blocking.

    This runs inside the request handler. A blocking 3-second POST would
    stall the entire event loop -- every other in-flight request included.
    Proven by running a second coroutine concurrently with a slow rerank
    and checking it made progress.
    """
    candidates = [_candidate("chunk")]
    progress = []

    async def ticker():
        for _ in range(5):
            await asyncio.sleep(0.01)
            progress.append(1)

    await asyncio.gather(
        rerank_with_fallback(FakeReranker(delay=0.15), "q", candidates, timeout=1.0),
        ticker(),
    )

    assert len(progress) == 5


async def test_top_n_comes_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "RERANK_TOP_N", 2)
    candidates = [_candidate(f"chunk {i}") for i in range(10)]

    ranked, _ = await rerank_with_fallback(FakeReranker(), "chunk", candidates)

    assert len(ranked) == 2


# ----------------------------------------------------------------- hosted ----


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def json(self):
        return self._payload


class _FakeHttpClient:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status
        self.requests: list[dict] = []

    async def post(self, url, *, json, headers):
        self.requests.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self._payload, self._status)


async def test_hosted_reranker_maps_indexes_back_to_candidates():
    candidates = [_candidate("first"), _candidate("second"), _candidate("third")]
    client = _FakeHttpClient(
        {"results": [{"index": 2, "relevance_score": 0.91}, {"index": 0, "relevance_score": 0.42}]}
    )
    reranker = HostedReranker(
        api_key="k", model="rerank-v3.5", base_url="https://x/rerank",
        timeout=1.0, client=client,
    )

    ranked = await reranker.rerank("q", candidates, 2)

    assert [r.candidate.content for r in ranked] == ["third", "first"]
    assert ranked[0].score == pytest.approx(0.91)
    assert [r.rank for r in ranked] == [1, 2]


async def test_hosted_reranker_sends_the_configured_model_and_url():
    client = _FakeHttpClient({"results": []})
    reranker = HostedReranker(
        api_key="secret", model="my-model", base_url="https://provider/v2/rerank",
        timeout=1.0, client=client,
    )

    await reranker.rerank("the question", [_candidate("body")], 1)

    request = client.requests[0]
    # Model name is config, never hardcoded.
    assert request["json"]["model"] == "my-model"
    assert request["url"] == "https://provider/v2/rerank"
    assert request["headers"]["Authorization"] == "Bearer secret"


async def test_hosted_reranker_rejects_an_out_of_range_index():
    client = _FakeHttpClient({"results": [{"index": 99, "relevance_score": 0.9}]})
    reranker = HostedReranker(
        api_key="k", model="m", base_url="u", timeout=1.0, client=client
    )

    # Attaching a score to the wrong chunk would silently corrupt the
    # ranking, and the citation that follows it.
    with pytest.raises(ValueError):
        await reranker.rerank("q", [_candidate("only one")], 1)


async def test_hosted_reranker_http_error_triggers_the_fallback():
    client = _FakeHttpClient({"results": []}, status=503)
    reranker = HostedReranker(
        api_key="k", model="m", base_url="u", timeout=1.0, client=client
    )
    candidates = [_candidate("a"), _candidate("b")]

    ranked, used_fallback = await rerank_with_fallback(reranker, "q", candidates, top_n=2)

    assert used_fallback is True
    assert len(ranked) == 2


def test_hosted_scores_are_calibrated():
    assert (
        HostedReranker(api_key="k", model="m", base_url="u", timeout=1.0, client=object())
        .scores_are_calibrated
        is True
    )


# ------------------------------------------------------- parent mapping ----


def _seed_parent_with_children(db_session, org, user, parent_text, child_texts):
    document = Document(
        id=uuid.uuid4(),
        org_id=org.id,
        uploaded_by=user.id,
        filename="handbook.pdf",
        storage_key=f"{org.id}/{uuid.uuid4()}/handbook.pdf",
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
        content=parent_text,
        page_number=4,
        token_count=50,
        chunk_index=0,
    )
    db_session.add(parent)
    db_session.flush()
    children = []
    for i, body in enumerate(child_texts):
        child = DocumentChunk(
            id=uuid.uuid4(),
            org_id=org.id,
            document_id=document.id,
            parent_id=parent.id,
            content=body,
            embedding=[0.0] * settings.EMBEDDING_DIMENSIONS,
            embedding_model="fake",
            page_number=4,
            token_count=10,
            chunk_index=i,
        )
        children.append(child)
    db_session.add_all(children)
    db_session.commit()
    return document, parent, children


def _ranked_from(children, scores):
    out = []
    for position, (child, score) in enumerate(zip(children, scores), start=1):
        out.append(
            RankedCandidate(
                candidate=Candidate(
                    chunk_id=child.id,
                    parent_id=child.parent_id,
                    document_id=child.document_id,
                    filename="handbook.pdf",
                    page_number=child.page_number,
                    content=child.content,
                    token_count=child.token_count,
                    rrf_score=0.016,
                    vector_rank=position,
                    text_rank=None,
                ),
                score=score,
                rank=position,
            )
        )
    return out


def test_several_children_of_one_parent_collapse_to_one_parent(
    db_session, org_factory, user_factory
):
    org = org_factory()
    user = user_factory(org)
    _, parent, children = _seed_parent_with_children(
        db_session, org, user, "The whole passage.", ["part one", "part two", "part three"]
    )

    parents = map_to_parents(db_session, _ranked_from(children, [0.9, 0.8, 0.7]), org_id=org.id)

    # Children overlap by design, so a strong passage produces several
    # strong children. Without dedupe one passage would occupy three
    # context slots and the LLM would read the repetition as corroboration.
    assert len(parents) == 1
    assert parents[0].parent_id == parent.id


def test_deduplicated_parent_keeps_the_best_rank(db_session, org_factory, user_factory):
    org = org_factory()
    user = user_factory(org)
    _, _, children = _seed_parent_with_children(
        db_session, org, user, "Passage.", ["a", "b"]
    )

    parents = map_to_parents(db_session, _ranked_from(children, [0.9, 0.5]), org_id=org.id)

    assert parents[0].best_rank == 1
    assert parents[0].best_score == pytest.approx(0.9)
    # The child that caused the selection is kept, so a citation can quote
    # the sentence that actually matched.
    assert parents[0].matched_chunk_id == children[0].id


def test_parents_are_ordered_by_best_rank(db_session, org_factory, user_factory):
    org = org_factory()
    user = user_factory(org)
    _, parent_a, children_a = _seed_parent_with_children(
        db_session, org, user, "A", ["a1"]
    )
    _, parent_b, children_b = _seed_parent_with_children(
        db_session, org, user, "B", ["b1"]
    )

    ranked = _ranked_from([children_b[0], children_a[0]], [0.9, 0.4])
    parents = map_to_parents(db_session, ranked, org_id=org.id)

    assert [p.parent_id for p in parents] == [parent_b.id, parent_a.id]


def test_parent_mapping_is_org_scoped(db_session, org_factory, user_factory):
    org_a, org_b = org_factory(), org_factory()
    user_a = user_factory(org_a)
    _, _, children = _seed_parent_with_children(db_session, org_a, user_a, "A", ["a1"])

    # Same ranked children, but asked for as org B: the tenant filter is
    # re-applied on every query touching tenant data, not just the first
    # in the chain.
    parents = map_to_parents(db_session, _ranked_from(children, [0.9]), org_id=org_b.id)

    assert parents == []


def test_parent_mapping_carries_citation_fields(db_session, org_factory, user_factory):
    org = org_factory()
    user = user_factory(org)
    document, _, children = _seed_parent_with_children(
        db_session, org, user, "Full passage text.", ["part"]
    )

    [parent] = map_to_parents(db_session, _ranked_from(children, [0.9]), org_id=org.id)

    assert parent.document_id == document.id
    assert parent.filename == "handbook.pdf"
    assert parent.page_number == 4
    assert parent.content == "Full passage text."
    assert parent.token_count == 50


def test_empty_ranked_list_maps_to_no_parents(db_session, org_factory):
    org = org_factory()
    assert map_to_parents(db_session, [], org_id=org.id) == []


# -------------------------------------------------------- confidence gate ----


def test_gate_closes_below_the_threshold():
    ranked = [
        RankedCandidate(candidate=_candidate("weakly related"), score=0.05, rank=1)
    ]

    assert (
        should_answer(ranked, reranker=FakeReranker(), used_fallback=False, threshold=0.3)
        is False
    )


def test_gate_opens_at_or_above_the_threshold():
    ranked = [RankedCandidate(candidate=_candidate("spot on"), score=0.3, rank=1)]

    assert (
        should_answer(ranked, reranker=FakeReranker(), used_fallback=False, threshold=0.3)
        is True
    )


def test_gate_closes_on_no_candidates():
    assert should_answer([], reranker=FakeReranker(), used_fallback=False) is False


def test_gate_opens_when_scores_are_uncalibrated():
    """RRF scores must never be compared to the threshold.

    RRF tops out near 1/(k+1) ~= 0.016. Testing that against 0.3 would
    gate EVERY question and the system would answer nothing.
    """
    ranked = [RankedCandidate(candidate=_candidate("x"), score=0.016, rank=1)]

    assert (
        should_answer(
            ranked, reranker=PassthroughReranker(), used_fallback=False, threshold=0.3
        )
        is True
    )


def test_gate_opens_after_a_fallback():
    # After a fallback the scores are RRF scores again, regardless of which
    # reranker was configured.
    ranked = [RankedCandidate(candidate=_candidate("x"), score=0.016, rank=1)]

    assert (
        should_answer(ranked, reranker=FakeReranker(), used_fallback=True, threshold=0.3)
        is True
    )


def test_gate_threshold_comes_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "CONFIDENCE_THRESHOLD", 0.95)
    ranked = [RankedCandidate(candidate=_candidate("x"), score=0.5, rank=1)]

    assert should_answer(ranked, reranker=FakeReranker(), used_fallback=False) is False


def test_not_found_answer_mentions_the_users_documents():
    # The wording matters: "I don't know" invites the user to rephrase,
    # while naming their documents says where the gap is.
    assert "documents" in NOT_FOUND_ANSWER.lower()

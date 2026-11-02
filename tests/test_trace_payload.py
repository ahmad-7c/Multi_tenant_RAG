"""B1: the honest trace and source payload on the `citations` SSE event.

The UI wants to show trace chips ("rerank 118 ms", "confidence 0.82"),
a relevance bar per source card, and the parent passage behind each
citation. Before B1 none of those numbers reached the client: the gate
score was computed and thrown away (`should_answer` returned a bool),
per-stage timings were never measured, and `Citation` had no score and
no passage.

Two rules drive every test here:

1. A number the backend did not produce is NEVER sent. Absent is `null`,
   never 0 and never a placeholder -- `candidates 0` on a cache hit
   would be a fabricated statistic, and the UI's honesty rule is to
   hide a null element rather than render a zero.
2. A rank-reciprocal score is NEVER exposed as relevance. RRF scores top
   out near 1/(k+1); shown in a 0..1 relevance bar they would read as
   "almost no match" for every source of every answer. Exposure is gated
   on one derived predicate, `scores_are_relevance`, which is the same
   expression the confidence gate itself branches on.

Fake embedder, fake reranker, fake LLM, in-memory cache: no network. The
database is real Postgres + pgvector, so retrieval and the org filter
are exercised for real.
"""
import json
import uuid

import pytest

from app.core.config import settings
from app.models.conversation import Conversation
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.organization import Organization
from app.models.user import UserRole
from app.services import query_service
from app.services.cache import CachedAnswer, InMemoryAnswerCache, answer_cache_key
from app.services.citations import PASSAGE_CHARS, Citation, validate_citations
from app.services.embedding import FakeEmbeddings
from app.services.llm import FakeLLM
from app.services.reranker import FakeReranker, PassthroughReranker

CHUNK_TEXTS = [
    "Receipts must be submitted within thirty days of travel.",
    "Expense claims above 500 USD require director approval.",
    "The per-diem limit for meals is 60 USD per day.",
]

QUESTION = "receipts submitted within thirty days of travel"

# Over the 1200-char cap and made of whole sentences, so the cap has a
# sentence boundary to cut on rather than falling back to a word break.
LONG_PARENT = " ".join(
    f"Clause {n} of the vendor agreement governs quarterly reconciliation duties."
    for n in range(1, 31)
)
# The child that retrieves is a short excerpt of its parent -- the real
# parent-child shape, and the reason `passage` is worth sending at all:
# the model saw the whole parent, the embedded child is a fragment of it.
LONG_CHILD = "Clause 1 of the vendor agreement governs quarterly reconciliation duties."
LONG_QUESTION = "quarterly reconciliation duties"

# A parent whose answer-bearing sentence sits PAST the cap, so a window
# anchored at the parent's start would exclude the very text that
# retrieved it.
LATE_MATCH_SENTENCE = (
    "Refunds for cancelled vendor engagements are issued within ninety "
    "days of written termination notice."
)
LATE_PARENT = (
    " ".join(
        f"Clause {n} of the vendor agreement governs quarterly reconciliation duties."
        for n in range(1, 25)
    )
    + " "
    + LATE_MATCH_SENTENCE
)
LATE_QUESTION = "refunds for cancelled vendor engagements ninety days"

# Appears in org A's corpus and nowhere else, so a cross-tenant leak
# through any path is a substring check away.
ORG_A_SECRET = "zarnex pentachloride settlement schedule"


@pytest.fixture(autouse=True)
def _keep_test_session_open(db_session, monkeypatch):
    """Neutralise close() on the shared test session.

    Production gives each blocking step its own session from the factory
    and closes it on a worker thread. Tests pass one transaction-scoped
    session instead, so a per-step close() would pull the rug out from
    under the rest of the test.
    """
    monkeypatch.setattr(db_session, "close", lambda: None)


@pytest.fixture()
def embedder():
    return FakeEmbeddings(dimensions=settings.EMBEDDING_DIMENSIONS)


async def _run_blocking(fn):
    """Run 'blocking' work inline.

    A worker thread cannot see this test's uncommitted transaction.
    """
    return fn()


def _seed(db_session, embedder, org, user, filename, bodies, *, child_of=None):
    """One document whose parents hold `bodies`.

    `child_of` maps a parent index to the child text to embed for it,
    defaulting to the parent's own text.
    """
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

    parents = []
    for index, body in enumerate(bodies):
        parent = DocumentChunk(
            id=uuid.uuid4(),
            org_id=org.id,
            document_id=document.id,
            parent_id=None,
            content=body,
            page_number=index + 1,
            token_count=len(body.split()),
            chunk_index=index,
        )
        db_session.add(parent)
        db_session.flush()

        child_text = (child_of or {}).get(index, body)
        [vector] = embedder.embed([child_text])
        db_session.add(
            DocumentChunk(
                id=uuid.uuid4(),
                org_id=org.id,
                document_id=document.id,
                parent_id=parent.id,
                content=child_text,
                embedding=vector,
                embedding_model=embedder.model,
                page_number=index + 1,
                token_count=len(child_text.split()),
                chunk_index=0,
            )
        )
        parents.append(parent)
    db_session.commit()
    return document, parents


@pytest.fixture()
def corpus(db_session, org_factory, user_factory, embedder):
    org = org_factory()
    user = user_factory(org, role=UserRole.ADMIN)
    document, parents = _seed(
        db_session, embedder, org, user, "expenses.pdf", CHUNK_TEXTS
    )
    return org, user, document, parents


def _conversation(db_session, org, user):
    conversation = Conversation(
        id=uuid.uuid4(), org_id=org.id, user_id=user.id, title="t"
    )
    db_session.add(conversation)
    db_session.commit()
    return conversation


async def _prepare(db_session, org, user, conversation, question, **overrides):
    kwargs = dict(
        session_factory=lambda: db_session,
        org_id=org.id,
        user_id=user.id,
        conversation_id=conversation.id,
        question=question,
        history=[],
        index_version=db_session.get(Organization, org.id).index_version,
        embedder=FakeEmbeddings(dimensions=settings.EMBEDDING_DIMENSIONS),
        reranker=FakeReranker(),
        rewrite_llm=FakeLLM(answer="rewritten"),
        cache=InMemoryAnswerCache(),
        run_blocking=_run_blocking,
    )
    kwargs.update(overrides)
    return await query_service.prepare_query(**kwargs)


async def _outcome(prepared, llm):
    """Drive phase 2 and return the final AnswerOutcome."""
    events = [item async for item in query_service.generate_answer(prepared, llm=llm)]
    assert events[-1][0] == "done"
    return events[-1][1]


def _payload(prepared, outcome):
    """The citations event the route would emit for this turn."""
    import app.api.v1.routes.conversations as route

    return route.build_citations_payload(prepared, outcome)


# ------------------------------------------------------- payload shape ----


async def test_the_citations_payload_carries_every_new_trace_field(db_session, corpus):
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    prepared = await _prepare(db_session, org, user, conversation, QUESTION)
    outcome = await _outcome(prepared, FakeLLM())

    payload = _payload(prepared, outcome)

    # The fields the UI's trace chips and source cards read.
    for field in (
        "confidence",
        "scores_calibrated",
        "used_reranker_fallback",
        "candidate_count",
        "parent_count",
        "retrieve_ms",
        "rerank_ms",
        "first_token_ms",
    ):
        assert field in payload, f"{field} missing from the citations event"


async def test_the_existing_citations_fields_keep_their_meaning(db_session, corpus):
    """B1 adds fields; it must not change what the old ones mean.

    The BFF and the eval harness both read these, so a silent change of
    meaning here is worse than a missing field: nothing would error.
    """
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    prepared = await _prepare(db_session, org, user, conversation, QUESTION)
    outcome = await _outcome(prepared, FakeLLM())

    payload = _payload(prepared, outcome)

    assert "thirty days" in payload["answer"]
    assert payload["cached"] is False
    assert payload["gated"] is False
    # None, and that is the pre-B1 meaning preserved: rule 1 of the
    # rewriter is "no history, no rewrite", so a first question is
    # never rewritten and the field stays null. The UI's 'rewritten'
    # chip is therefore absent on a conversation's first turn.
    assert payload["rewritten_question"] is None

    citation = payload["citations"][0]
    assert citation["marker"] == 1
    assert citation["document"] == "expenses.pdf"
    assert citation["page"] == 1
    assert "thirty days" in citation["snippet"]
    uuid.UUID(citation["document_id"])
    uuid.UUID(citation["chunk_id"])


async def test_rewritten_question_is_published_once_history_exists(db_session, corpus):
    """The 'rewritten' trace chip needs the field to populate somewhere."""
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    prepared = await _prepare(
        db_session,
        org,
        user,
        conversation,
        "what about receipts",
        history=[
            {"role": "user", "content": "how do expenses work"},
            {"role": "assistant", "content": "Claims need approval."},
        ],
        rewrite_llm=FakeLLM(answer="rewritten"),
    )
    outcome = await _outcome(prepared, FakeLLM())

    payload = _payload(prepared, outcome)

    assert payload["rewritten_question"] == "rewritten"


# ------------------------------------------- calibrated vs rank-reciprocal ----


async def test_scores_and_confidence_are_exposed_when_the_reranker_is_calibrated(
    db_session, corpus
):
    """The positive control for the two null tests below."""
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    prepared = await _prepare(
        db_session, org, user, conversation, QUESTION, reranker=FakeReranker()
    )
    outcome = await _outcome(prepared, FakeLLM())

    payload = _payload(prepared, outcome)

    assert payload["scores_calibrated"] is True
    assert payload["used_reranker_fallback"] is False
    assert isinstance(payload["confidence"], float)
    assert 0.0 <= payload["confidence"] <= 1.0
    score = payload["citations"][0]["score"]
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0


async def test_no_scores_under_passthrough_because_rrf_is_not_relevance(
    db_session, corpus, monkeypatch
):
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    # The floor is what gates an uncalibrated run; 0 disables it so this
    # test measures the payload rather than the gate.
    monkeypatch.setattr(settings, "VECTOR_SIMILARITY_FLOOR", 0.0)
    prepared = await _prepare(
        db_session, org, user, conversation, QUESTION, reranker=PassthroughReranker()
    )
    outcome = await _outcome(prepared, FakeLLM())

    payload = _payload(prepared, outcome)

    assert payload["scores_calibrated"] is False
    assert payload["confidence"] is None
    assert payload["citations"], "expected an answer to assert scores on"
    assert all(c["score"] is None for c in payload["citations"])


async def test_no_scores_under_reranker_fallback_even_though_hosted_is_calibrated(
    db_session, corpus, monkeypatch
):
    """The subtle half.

    A hosted reranker reports `scores_are_calibrated = True` as a
    property of the PROVIDER, but when the call fails the ranking that
    comes back is RRF order with RRF scores. Gating exposure on the
    provider flag alone would publish rank reciprocals as relevance
    during exactly the outage nobody is watching.
    """
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    monkeypatch.setattr(settings, "VECTOR_SIMILARITY_FLOOR", 0.0)
    # fail=True: calibrated provider, failed call -> RRF fallback.
    failing = FakeReranker(scores_calibrated=True, fail=True)
    prepared = await _prepare(
        db_session, org, user, conversation, QUESTION, reranker=failing
    )
    outcome = await _outcome(prepared, FakeLLM())

    payload = _payload(prepared, outcome)

    assert payload["used_reranker_fallback"] is True
    assert payload["scores_calibrated"] is False
    assert payload["confidence"] is None
    assert payload["citations"], "expected an answer to assert scores on"
    assert all(c["score"] is None for c in payload["citations"])


# -------------------------------------------------------------- timings ----


async def test_timings_are_measured_on_a_live_answer(db_session, corpus):
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    prepared = await _prepare(db_session, org, user, conversation, QUESTION)
    # A per-chunk delay makes time-to-first-token observably non-zero
    # rather than a number that happens to round to 0 on a fast box.
    outcome = await _outcome(prepared, FakeLLM(delay=0.02))

    payload = _payload(prepared, outcome)

    assert isinstance(payload["retrieve_ms"], int) and payload["retrieve_ms"] >= 0
    assert isinstance(payload["rerank_ms"], int) and payload["rerank_ms"] >= 0
    assert payload["first_token_ms"] >= 10, payload["first_token_ms"]
    assert isinstance(payload["candidate_count"], int)
    assert payload["candidate_count"] > 0
    assert payload["parent_count"] > 0


async def test_a_cache_hit_invents_nothing(db_session, corpus):
    """A replay did no retrieval, no rerank and no generation.

    Every one of those numbers is absent, not zero. `candidates 0` and
    `rerank 0 ms` are both false statements about this request, and the
    second one would read as an impossibly fast rerank rather than as
    "there wasn't one".
    """
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    cache = InMemoryAnswerCache()
    index_version = db_session.get(Organization, org.id).index_version
    # Keyed on the question AS REWRITTEN, which with no history is the
    # raw question unchanged.
    key = answer_cache_key(
        org_id=org.id, question=QUESTION, index_version=index_version
    )
    await cache.set(
        key,
        CachedAnswer(
            answer="Receipts are due within thirty days [1].",
            citations=[
                {
                    "marker": 1,
                    "document_id": str(uuid.uuid4()),
                    "chunk_id": str(uuid.uuid4()),
                    "document": "expenses.pdf",
                    "page": 1,
                    "snippet": "Receipts must be submitted within thirty days.",
                    "passage": "Receipts must be submitted within thirty days of travel.",
                    "matched_chunk_id": str(uuid.uuid4()),
                    "score": 0.91,
                }
            ],
            model="fake-llm",
            cached_at=0.0,
        ),
        60,
    )

    prepared = await _prepare(
        db_session, org, user, conversation, QUESTION, cache=cache
    )
    outcome = await _outcome(prepared, FakeLLM())

    payload = _payload(prepared, outcome)

    assert payload["cached"] is True
    assert payload["retrieve_ms"] is None
    assert payload["rerank_ms"] is None
    assert payload["first_token_ms"] is None
    assert payload["candidate_count"] is None
    assert payload["parent_count"] is None
    # Nothing was scored for THIS request, so no score is published --
    # even though the cached entry carries one from the run that filled
    # it, under a reranker configuration we can no longer verify.
    assert payload["confidence"] is None
    assert payload["scores_calibrated"] is False
    assert payload["citations"][0]["score"] is None
    # The passage still replays: it is real text, and the cache key
    # includes org_id so it can only be served back to its own org.
    assert "thirty days" in payload["citations"][0]["passage"]


async def test_a_gated_refusal_keeps_its_retrieval_timings(
    db_session, corpus, monkeypatch
):
    """The gate refused, so there is no first token -- but there WAS a
    retrieve and a rerank, and their cost is real and worth showing."""
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    monkeypatch.setattr(settings, "CONFIDENCE_THRESHOLD", 0.99)
    # Off topic, so the fake reranker's overlap score is genuinely low
    # rather than the threshold doing all the work.
    prepared = await _prepare(
        db_session,
        org,
        user,
        conversation,
        "zygomatic defenestration of the quux subsystem",
    )
    outcome = await _outcome(prepared, FakeLLM())

    payload = _payload(prepared, outcome)

    assert payload["gated"] is True
    assert isinstance(payload["retrieve_ms"], int)
    assert isinstance(payload["rerank_ms"], int)
    assert payload["first_token_ms"] is None
    assert payload["candidate_count"] > 0
    # Parents are mapped only past the gate, so this one never happened.
    assert payload["parent_count"] is None
    # The gate score is the most useful number on a refusal: it says how
    # close the best match came.
    assert isinstance(payload["confidence"], float)


async def test_a_failed_stream_publishes_no_timing(db_session, corpus):
    """Partial text is discarded on failure, so its timing goes too.

    Reporting a first-token time for an answer the user never received
    describes a stream that was abandoned as if it had been delivered.
    """
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    prepared = await _prepare(db_session, org, user, conversation, QUESTION)
    outcome = await _outcome(prepared, FakeLLM(error=RuntimeError("provider exploded")))

    payload = _payload(prepared, outcome)

    assert outcome.errored is True
    assert payload["first_token_ms"] is None
    assert payload["citations"] == []
    # Retrieval and rerank still happened and still cost something.
    assert isinstance(payload["retrieve_ms"], int)


# -------------------------------------------------------------- passages ----


async def test_passage_is_sent_only_for_cited_sources(db_session, corpus):
    """Three blocks go to the model; one is cited.

    The uncited parents' text must not ride along. They are the model's
    rejected context, the user never saw them, and shipping them would
    quietly widen what one question discloses.
    """
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    prepared = await _prepare(db_session, org, user, conversation, QUESTION)
    assert len(prepared.prompt.blocks) > 1, "need several blocks for this to mean anything"

    # Cites [1] only.
    outcome = await _outcome(
        prepared, FakeLLM(answer="Receipts are due within thirty days [1].")
    )

    payload = _payload(prepared, outcome)

    assert len(payload["citations"]) == 1
    assert payload["citations"][0]["passage"]

    # No uncited block's content anywhere in the serialised event.
    raw = json.dumps(payload)
    cited_parent_id = str(payload["citations"][0]["chunk_id"])
    for block in prepared.prompt.blocks:
        if str(block.parent_id) == cited_parent_id:
            continue
        assert block.content not in raw, f"uncited passage leaked: {block.content!r}"


async def test_passage_is_capped_and_cut_at_a_sentence_boundary(
    db_session, org_factory, user_factory, embedder
):
    org = org_factory()
    user = user_factory(org, role=UserRole.ADMIN)
    _seed(
        db_session,
        embedder,
        org,
        user,
        "vendor.pdf",
        [LONG_PARENT],
        child_of={0: LONG_CHILD},
    )
    conversation = _conversation(db_session, org, user)
    prepared = await _prepare(db_session, org, user, conversation, LONG_QUESTION)
    outcome = await _outcome(
        prepared, FakeLLM(answer="Reconciliation is quarterly [1].")
    )

    payload = _payload(prepared, outcome)
    passage = payload["citations"][0]["passage"]

    assert len(LONG_PARENT) > PASSAGE_CHARS, "fixture must exceed the cap to test it"
    assert len(passage) <= PASSAGE_CHARS
    # Cut at a sentence end, not mid-word and not mid-sentence.
    assert passage.endswith(".")
    assert LONG_PARENT.startswith(passage.rstrip())


async def test_matched_chunk_id_names_the_child_that_retrieved(
    db_session, org_factory, user_factory, embedder
):
    """The child is what matched; the parent is what the model read.

    The UI highlights the matched sentence inside the passage, so it
    needs to know which child caused the parent to be selected.
    """
    org = org_factory()
    user = user_factory(org, role=UserRole.ADMIN)
    _, parents = _seed(
        db_session,
        embedder,
        org,
        user,
        "vendor.pdf",
        [LONG_PARENT],
        child_of={0: LONG_CHILD},
    )
    conversation = _conversation(db_session, org, user)
    prepared = await _prepare(db_session, org, user, conversation, LONG_QUESTION)
    outcome = await _outcome(
        prepared, FakeLLM(answer="Reconciliation is quarterly [1].")
    )

    payload = _payload(prepared, outcome)
    matched = payload["citations"][0]["matched_chunk_id"]

    child = (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.parent_id == parents[0].id)
        .one()
    )
    assert matched == str(child.id)
    # And it is a child, not the parent that was cited.
    assert matched != payload["citations"][0]["chunk_id"]


async def test_the_passage_window_follows_the_matched_chunk_past_the_cap(
    db_session, org_factory, user_factory, embedder
):
    """The window follows the match, not the start of the parent.

    A parent runs to ~500 tokens, comfortably more than PASSAGE_CHARS,
    so for any match in the back half a start-anchored window shows the
    reader the WRONG PART of the right page: the highlight resolves to
    nothing and the excerpt visibly fails to contain the answer sitting
    above it. The reader's conclusion is that the citation is wrong,
    which is the opposite of what the passage is for.
    """
    org = org_factory()
    user = user_factory(org, role=UserRole.ADMIN)
    _seed(
        db_session,
        embedder,
        org,
        user,
        "vendor.pdf",
        [LATE_PARENT],
        child_of={0: LATE_MATCH_SENTENCE},
    )
    conversation = _conversation(db_session, org, user)
    prepared = await _prepare(db_session, org, user, conversation, LATE_QUESTION)
    outcome = await _outcome(prepared, FakeLLM(answer="Ninety days [1]."))

    payload = _payload(prepared, outcome)
    passage = payload["citations"][0]["passage"]

    assert LATE_PARENT.index(LATE_MATCH_SENTENCE) > PASSAGE_CHARS, (
        "fixture must place the matched child past the cap or this test "
        "proves nothing"
    )
    assert len(passage) <= PASSAGE_CHARS
    assert LATE_MATCH_SENTENCE in passage
    # A window that moved is marked as an excerpt, so the reader knows
    # text precedes it rather than assuming the parent starts here.
    assert passage.startswith("...")


async def test_deleting_a_document_makes_its_cached_passage_unreachable(
    client, db_session, corpus, auth_headers
):
    """Cache invalidation is now a DISCLOSURE boundary, not just a
    freshness one.

    Before B1 a stale cache entry held an answer and a 300-char snippet.
    It now holds up to 1200 characters of a document's text per
    citation, so "the corpus changed" and "this text may no longer be
    shown" became the same question. The key hashes org_id + question +
    index_version, and `delete_document` bumps index_version in the SAME
    transaction as the delete -- so the key a cached passage lives under
    can no longer be COMPUTED by any later request. The entry is
    unreachable, not merely stale, which is the property that matters
    when the payload is document text.

    (`test_documents.py::test_delete_increments_index_version` pins the
    bump itself. This pins the consequence for passages.)
    """
    org, user, document, _ = corpus
    index_before = db_session.get(Organization, org.id).index_version

    cache = InMemoryAnswerCache()
    key_before = answer_cache_key(
        org_id=org.id, question=QUESTION, index_version=index_before
    )
    await cache.set(
        key_before,
        CachedAnswer(
            answer="Receipts are due within thirty days [1].",
            citations=[
                {
                    "marker": 1,
                    "document_id": str(document.id),
                    "chunk_id": str(uuid.uuid4()),
                    "document": "expenses.pdf",
                    "page": 1,
                    "snippet": ORG_A_SECRET,
                    "passage": f"The {ORG_A_SECRET} is reviewed quarterly.",
                    "matched_chunk_id": str(uuid.uuid4()),
                    "score": 0.9,
                }
            ],
            model="fake-llm",
            cached_at=0.0,
        ),
        60,
    )
    assert await cache.get(key_before) is not None, "fixture must be warm"

    deleted = client.delete(
        f"/api/v1/documents/{document.id}", headers=auth_headers(user)
    )
    assert deleted.status_code == 204

    db_session.expire_all()
    index_after = db_session.get(Organization, org.id).index_version
    assert index_after == index_before + 1

    # The question is identical and the org is identical; only the index
    # version moved, and that is enough to make the old key uncomputable.
    key_after = answer_cache_key(
        org_id=org.id, question=QUESTION, index_version=index_after
    )
    assert key_after != key_before
    assert await cache.get(key_after) is None

    # And end to end: a real prepare for the same question now misses,
    # so the deleted document's passage cannot reach a client.
    conversation = _conversation(db_session, org, user)
    prepared = await _prepare(
        db_session, org, user, conversation, QUESTION, cache=cache
    )
    assert prepared.cache_hit is False
    assert ORG_A_SECRET not in json.dumps(
        _payload(prepared, await _outcome(prepared, FakeLLM()))
    )


# --------------------------------------------- each gate site, on its own ----


def _ctx_block(marker=1, *, content="Receipts are due in thirty days.", score=0.87):
    from app.services.prompting import ContextBlock

    return ContextBlock(
        marker=marker,
        parent_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="expenses.pdf",
        page_number=1,
        content=content,
        matched_chunk_id=uuid.uuid4(),
        score=score,
    )


def test_validate_citations_withholds_the_score_unless_asked():
    """Site 1 of the two places a score is gated.

    The score is nulled twice -- once where the Citation is built and
    again where the payload is serialised -- so that neither is
    load-bearing alone. Redundancy is only worth having if both halves
    actually work, so each half is pinned by its own test. Without this,
    breaking one half is invisible and the "defence in depth" is one
    layer deep without anyone knowing which layer.
    """
    blocks = [_ctx_block()]

    _, withheld = validate_citations("Claim [1].", blocks)
    _, exposed = validate_citations("Claim [1].", blocks, expose_scores=True)

    assert withheld[0].score is None
    assert exposed[0].score == 0.87
    # The passage is not gated on scores: it is the document's own text,
    # not a statistic that can be misread.
    assert withheld[0].passage == "Receipts are due in thirty days."


def test_the_citation_payload_withholds_the_score_unless_exposed():
    """Site 2 of the two. Catches a cached score on replay."""
    import app.api.v1.routes.conversations as route

    citation = Citation(
        marker=1,
        document_id=uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        document="expenses.pdf",
        page=1,
        snippet="s",
        passage="p",
        matched_chunk_id=uuid.uuid4(),
        score=0.87,
    )

    assert route._citation_payload(citation, expose_score=False)["score"] is None
    assert route._citation_payload(citation, expose_score=True)["score"] == 0.87


# ------------------------------------------------------------- isolation ----


async def test_org_b_cannot_obtain_org_a_passages_through_retrieval(
    db_session, org_factory, user_factory, embedder
):
    """A BACKSTOP, and measured as one -- know what it does before
    trusting it.

    There are FOUR tenant filters between org B's question and a
    passage: the vector leg, the keyword leg, retrieval's hydration
    fetch, and `map_to_parents`. They are mutually redundant, so this
    test was confirmed to still PASS with any single one of them removed
    and to fail only once all four are gone. It proves the layers do not
    leak TOGETHER; it does not pin any one of them, and it should not be
    read as covering them.

    Each layer is pinned individually, elsewhere, by a test that
    executes that layer's own statement so no other layer can pass on
    its behalf (B1b):

      - vector leg      test_retrieval.py::test_the_vector_leg_alone_is_org_scoped
      - keyword leg     test_retrieval.py::test_the_keyword_leg_alone_is_org_scoped
      - hydration fetch test_retrieval.py::test_the_hydration_fetch_alone_is_org_scoped
      - map_to_parents  test_reranker.py::test_parent_mapping_is_org_scoped

    All four were confirmed failing against the removal of their own
    filter and passing against the other three, so the coverage is one
    test per filter with no overlap. No leak was found: every filter was
    present and correct, and the gap was entirely in what the suite
    pinned.
    """
    org_a = org_factory()
    user_a = user_factory(org_a, role=UserRole.ADMIN)
    _seed(
        db_session,
        embedder,
        org_a,
        user_a,
        "secret-a.pdf",
        [f"The {ORG_A_SECRET} is reviewed every quarter by the board."],
    )

    org_b = org_factory()
    user_b = user_factory(org_b, role=UserRole.ADMIN)
    _seed(db_session, embedder, org_b, user_b, "policy-b.pdf", CHUNK_TEXTS)

    conversation = _conversation(db_session, org_b, user_b)
    # Org B asks for org A's content by name.
    prepared = await _prepare(
        db_session, org_b, user_b, conversation, f"what is the {ORG_A_SECRET}"
    )
    outcome = await _outcome(prepared, FakeLLM(answer="See the policy [1]."))

    payload = _payload(prepared, outcome)

    assert ORG_A_SECRET not in json.dumps(payload)


async def test_org_b_cannot_obtain_org_a_passages_through_the_cache(
    db_session, org_factory, user_factory, embedder
):
    """The other path to a passage.

    `passage` replays from the answer cache, so the cache is a second
    way a parent's text can reach a client. The key includes org_id,
    which is what makes that safe -- asserted here rather than assumed,
    because the two orgs ask the identical question and would otherwise
    collide on the question text alone.
    """
    org_a = org_factory()
    user_a = user_factory(org_a, role=UserRole.ADMIN)
    org_b = org_factory()
    user_b = user_factory(org_b, role=UserRole.ADMIN)
    _seed(db_session, embedder, org_b, user_b, "policy-b.pdf", CHUNK_TEXTS)

    cache = InMemoryAnswerCache()
    # Org A's answer, cached under org A's key, carrying org A's passage.
    await cache.set(
        answer_cache_key(
            org_id=org_a.id,
            question=QUESTION,
            index_version=db_session.get(Organization, org_a.id).index_version,
        ),
        CachedAnswer(
            answer="The schedule is quarterly [1].",
            citations=[
                {
                    "marker": 1,
                    "document_id": str(uuid.uuid4()),
                    "chunk_id": str(uuid.uuid4()),
                    "document": "secret-a.pdf",
                    "page": 1,
                    "snippet": ORG_A_SECRET,
                    "passage": f"The {ORG_A_SECRET} is reviewed every quarter.",
                    "matched_chunk_id": str(uuid.uuid4()),
                    "score": 0.9,
                }
            ],
            model="fake-llm",
            cached_at=0.0,
        ),
        60,
    )

    conversation = _conversation(db_session, org_b, user_b)
    prepared = await _prepare(
        db_session, org_b, user_b, conversation, QUESTION, cache=cache
    )
    outcome = await _outcome(prepared, FakeLLM())

    payload = _payload(prepared, outcome)

    assert prepared.cache_hit is False, "org B must not hit org A's cache entry"
    assert ORG_A_SECRET not in json.dumps(payload)


# ------------------------------------------------------ cache compatibility ----


def test_citations_cached_before_b1_still_load():
    """Pre-B1 cache entries have none of the new keys.

    `maybe_cache_answer` stores `asdict(Citation)` and the read path does
    `Citation(**c)`, so a required new field would raise TypeError on
    every entry written before this change -- turning a warm cache into
    an outage on deploy.
    """
    old = {
        "marker": 1,
        "document_id": str(uuid.uuid4()),
        "chunk_id": str(uuid.uuid4()),
        "document": "expenses.pdf",
        "page": 2,
        "snippet": "Receipts must be submitted within thirty days.",
    }

    citation = Citation(**old)

    assert citation.passage is None
    assert citation.score is None
    assert citation.matched_chunk_id is None


# ------------------------------------------- the published contract ----


async def test_the_openapi_schema_matches_what_the_route_actually_emits(
    db_session, corpus
):
    """`CitationsEvent` is documentation, so it can drift into fiction.

    The event is serialised from a plain dict (a StreamingResponse
    cannot carry a response_model without buffering the stream), which
    means nothing mechanically ties the published schema to the payload.
    This is that tie: add a field to one and this test names the other.
    """
    from app.schemas.conversation import CitationPayload, CitationsEvent

    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    prepared = await _prepare(db_session, org, user, conversation, QUESTION)
    outcome = await _outcome(prepared, FakeLLM())

    payload = _payload(prepared, outcome)

    assert set(payload) == set(CitationsEvent.model_fields)
    assert payload["citations"], "need a citation to compare its shape"
    assert set(payload["citations"][0]) == set(CitationPayload.model_fields)


def test_first_token_ms_is_documented_as_model_time_not_perceived_time():
    """The field is easy to mislabel in a UI, so the contract says so.

    A client that labels this "time to first text" is wrong by however
    long auth, retrieval and rerank took, plus the network -- and the UI
    is going to put it on a chip next to two other timings.
    """
    from app.schemas.conversation import CitationsEvent

    description = CitationsEvent.model_fields["first_token_ms"].description

    assert "MODEL time-to-first-token" in description
    assert "NOT user-perceived" in description
    assert "client-side" in description


async def test_scores_are_not_called_calibrated_when_nothing_was_scored(
    db_session, org_factory, user_factory, monkeypatch
):
    """Found by the U1 live run against an empty corpus.

    `scores_calibrated` answers "are the scores on THIS event real
    relevance values". With an empty corpus the ranking is empty, so
    there are no scores at all -- and the flag was reporting True,
    because `scores_are_relevance` only inspects the reranker's config
    and whether the call fell back, neither of which knows that nothing
    came back to score.

    Nothing false was displayed (confidence was null and there were no
    citations, so the UI had nothing to render), which is exactly why
    this needed a test rather than a bug report: the flag contradicted
    its own documented meaning while every visible value stayed honest.
    It also disagreed with the cache-hit path, which reports False for
    the same reason -- nothing was scored for this request.
    """
    org = org_factory()
    user = user_factory(org, role=UserRole.ADMIN)
    # No documents: retrieval returns nothing and the gate refuses on an
    # empty ranking.
    conversation = _conversation(db_session, org, user)
    prepared = await _prepare(db_session, org, user, conversation, QUESTION)
    outcome = await _outcome(prepared, FakeLLM())

    payload = _payload(prepared, outcome)

    assert payload["candidate_count"] == 0, "expected an empty corpus"
    assert payload["gated"] is True
    assert payload["confidence"] is None
    # The claim under test.
    assert payload["scores_calibrated"] is False


# ------------------------------------------- conversational turns ----


async def test_a_greeting_is_answered_without_retrieval_or_a_model_call(
    db_session, corpus
):
    """The transcript that prompted this: "hey how are you" answered
    with "I could not find anything in your documents".

    A greeting is not a question about a corpus. It must not reach the
    embedder, the reranker or the model -- partly because the reply
    would be wrong, and partly because every one of those is a billed
    network call for someone saying hello.
    """
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    embedder = FakeEmbeddings(dimensions=settings.EMBEDDING_DIMENSIONS)
    reranker = FakeReranker()
    llm = FakeLLM()

    prepared = await _prepare(
        db_session, org, user, conversation, "hey how are you",
        embedder=embedder, reranker=reranker,
    )
    outcome = await _outcome(prepared, llm)
    payload = _payload(prepared, outcome)

    assert prepared.assisted is True
    assert payload["kind"] == "assist"
    # Not a refusal: counting greetings as refusals would inflate the
    # refusal rate on the usage dashboard.
    assert payload["gated"] is False
    assert "could not find" not in payload["answer"].lower()

    # Nothing was spent.
    assert embedder.call_count == 0, "a greeting must not be embedded"
    assert reranker.calls == [], "a greeting must not be reranked"
    assert llm.calls == [], "a greeting must not reach the model"

    # And no measurement is invented for work that did not happen.
    assert payload["retrieve_ms"] is None
    assert payload["rerank_ms"] is None
    assert payload["first_token_ms"] is None
    assert payload["candidate_count"] is None
    assert payload["citations"] == []


async def test_a_capability_question_names_the_orgs_own_documents(
    db_session, corpus
):
    """"What can you do" is the question a stuck user actually asks.

    Answering it with the org's real filenames turns a dead end into a
    starting point -- and the names come from the caller's own tenant,
    through the same org-scoped query everything else uses.
    """
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)

    prepared = await _prepare(
        db_session, org, user, conversation, "what can you do"
    )
    outcome = await _outcome(prepared, FakeLLM())
    payload = _payload(prepared, outcome)

    assert payload["kind"] == "assist"
    assert "expenses.pdf" in payload["answer"]


async def test_a_capability_question_never_names_another_orgs_documents(
    db_session, org_factory, user_factory, embedder
):
    """The assist path reads the documents table, so it is one more
    place the tenant filter has to hold."""
    org_a = org_factory()
    user_a = user_factory(org_a, role=UserRole.ADMIN)
    _seed(db_session, embedder, org_a, user_a, "secret-a.pdf", CHUNK_TEXTS)

    org_b = org_factory()
    user_b = user_factory(org_b, role=UserRole.ADMIN)
    _seed(db_session, embedder, org_b, user_b, "policy-b.pdf", CHUNK_TEXTS)

    conversation = _conversation(db_session, org_b, user_b)
    prepared = await _prepare(
        db_session, org_b, user_b, conversation, "what documents do you have"
    )
    outcome = await _outcome(prepared, FakeLLM())
    payload = _payload(prepared, outcome)

    assert "policy-b.pdf" in payload["answer"]
    assert "secret-a.pdf" not in json.dumps(payload)


async def test_a_real_question_is_still_answered_normally(db_session, corpus):
    """The guard that matters: the intercept must not swallow work.

    A question that merely LOOKS conversational at a glance still goes
    through retrieval and comes back cited.
    """
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)

    prepared = await _prepare(db_session, org, user, conversation, QUESTION)
    outcome = await _outcome(prepared, FakeLLM())
    payload = _payload(prepared, outcome)

    assert prepared.assisted is False
    assert payload["kind"] == "answer"
    assert payload["citations"], "a real question must still be answered"


async def test_an_answer_with_a_passage_can_actually_be_cached(db_session, corpus):
    """A cache write that fails is INVISIBLE, so it needs a test.

    `maybe_cache_answer` swallows its own errors by design -- a cache
    is an optimisation and must never break a request the user already
    received. The cost of that is silence: when B1 added
    `matched_chunk_id` (a UUID) the hand-written serialiser did not
    stringify it, every write raised "Object of type UUID is not JSON
    serializable", and the only symptom was that nothing was ever
    cached. Found in an end-to-end log, not by a user.

    This asserts the write SUCCEEDS and the entry can be read back and
    rehydrated into a Citation.
    """
    org, user, _, _ = corpus
    conversation = _conversation(db_session, org, user)
    cache = InMemoryAnswerCache()

    prepared = await _prepare(
        db_session, org, user, conversation, QUESTION, cache=cache
    )
    outcome = await _outcome(prepared, FakeLLM())
    assert outcome.citations, "need a cited answer to cache"
    # The citation must carry the UUID field that broke this.
    assert outcome.citations[0].matched_chunk_id is not None

    await query_service.maybe_cache_answer(
        prepared,
        answer=outcome.answer,
        citations=outcome.citations,
        cache=cache,
        model="fake-llm",
        errored=False,
    )

    stored = await cache.get(prepared.cache_key)
    assert stored is not None, "the answer was not cached at all"

    # It round-trips: the read path does Citation(**entry), so a field
    # that failed to serialise would fail here too.
    rehydrated = [Citation(**c) for c in stored.citations]
    assert rehydrated[0].passage
    assert rehydrated[0].matched_chunk_id

    # And it is JSON, not Python objects pretending to be JSON.
    import json as _json

    _json.dumps(stored.citations)

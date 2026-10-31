"""Run the golden set against the live pipeline and calibrate the gate.

    venv/Scripts/python.exe -m eval.calibrate                 # everything
    venv/Scripts/python.exe -m eval.calibrate --no-generate   # scores only

Two thresholds come out of this, because the gate has two regimes and
only one of them is normally visible:

  * RERANKER score -- what `should_answer` compares against
    CONFIDENCE_THRESHOLD when the cross-encoder ran.
  * VECTOR similarity -- a FLOOR for when the reranker is down. Today
    `used_fallback=True` opens the gate unconditionally (reranker.py:663),
    because RRF scores top out near 0.016 and testing them against 0.3
    would refuse every question. So a dead reranker currently means
    answer everything. A cosine floor is the only calibrated number
    still available in that regime.

Cost discipline: the gate's decision depends on the reranker SCORE, not
on the answer, so the whole precision/recall curve comes from
retrieve+rerank alone. Generation runs only where an answer is actually
scored -- answerable, synthesis, and the isolation probes, which must be
checked for leaked content and not merely for refusing.
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text as sql_text

from app.core.config import settings
from app.core.http import aclose_async_client
from app.db.session import SessionLocal
from app.models.conversation import Conversation, Message, MessageKind, MessageRole
from app.models.document import Document, DocumentStatus
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.core.security import hash_password
from app.services import query_service
from app.services.cache import InMemoryAnswerCache
from app.services.chunking import chunk_pages
from app.services.cleaning import clean_pages
from app.services.embedding import EmbeddingTask, get_embedding_provider
from app.services.extraction import Page, extract_pages
from app.services.llm import get_judge_llm, get_llm, get_rewrite_llm
from app.services.reranker import (
    Reranker,
    get_reranker,
    map_to_parents,
    rerank_with_fallback,
)
from app.services.retrieval import retrieve_candidates
from eval.loader import GATE_EXCLUDED, load_golden_set, summarise
from eval.judge import (
    JudgeCache,
    judge_answer,
    load_hand_grades,
    score_agreement,
)
from eval.metrics import (
    CaseRetrieval,
    RankedItem,
    RetrievalTarget,
    citation_pages_correct,
    first_relevant_rank,
    recall_at_k,
    reciprocal_rank,
    summarise_retrieval,
)
from eval.runs import config_snapshot, write_run
from eval.scoring import check_citation_pages, check_phrases, check_rubric
from eval.thresholds import (
    ScoredCase,
    confusion,
    select_threshold,
)
from eval.thresholds import score_table as sorted_score_table

# The old single-file dump. Kept only as a name for the legacy file so
# `eval/runs.py`'s loader can still read a pre-snapshot run; nothing
# writes it any more. See the module docstring in eval/runs.py for why
# one fixed filename holding a bare list was unusable.
LEGACY_RESULTS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "results.json"
)

# A Cohere TRIAL key allows 10 rerank requests per minute. 26 cases fired
# back to back would mostly 429, fall back to RRF order, and report
# `used_fallback` -- which would silently poison Curve A with the one
# regime it is not measuring. Pacing is correct HERE and wrong in the
# request path: this is a batch job where wall-clock is free, whereas a
# user's query has a 3-second budget.
DEFAULT_PACE_SECONDS = 7.0

# `--smoke`: one case per RETRIEVAL SHAPE and one per gate regime, nine
# in all, chosen so that a smoke run exercises every code path a full
# run does rather than nine cases of the same kind.
#
#   expense-window        a plain fact on a PDF page, and the twin for
#                         the isolation probe below -- an isolation
#                         case whose twin is absent proves nothing
#   perdiem-international  a table row
#   rollback-command       a fenced code block
#   vsr-sponsor-duties     support straddling a child boundary, so the
#                          parent-child promotion has to work
#   approval-7000          the only path through check_rubric; without
#                          it --smoke never runs the negation scorer
#   mileage-motorcycle     a NEAR MISS: the gate's real job
#   vpn-setup              off-topic, the easy control
#   globex-asks-expense-window  the isolation direction globex -> acme
#   followup-above-that    history, so the query rewriter runs
#
# Deliberately NOT covered, and a --smoke report says so: exact_term,
# the acme -> globex isolation direction, and the ambiguous bucket. A
# smoke run is for "is the harness working and roughly right", never
# for a threshold.
SMOKE_IDS = (
    "acme-expense-window",
    "acme-perdiem-international",
    "acme-rollback-command",
    "acme-vsr-sponsor-duties",
    "acme-approval-7000",
    "acme-mileage-motorcycle",
    "acme-vpn-setup",
    "globex-asks-expense-window",
    "acme-followup-above-that",
)

# RERANKER_TIMEOUT_SECONDS is 3s because that is a USER's budget: past
# it, RRF order now beats waiting. The eval has the opposite need -- a
# timeout here means the case is scored on RRF instead of the
# cross-encoder, so the run would calibrate the fallback regime while
# claiming to calibrate the gate. Measured in a first pass: 3s timed out
# on roughly a third of cases.
DEFAULT_RERANK_TIMEOUT = 25.0

# 10 rerank req/min on a Cohere trial key. 6.5s spacing leaves a
# little headroom; sitting exactly on a published limit turns every
# jitter spike into a 429.
DEFAULT_RERANK_MIN_INTERVAL = 6.5

# One cache for the whole run. Keyed on the ANSWER's content, so a rerun
# that produces the same answers pays nothing and a rerun that produces
# different ones is judged again -- see eval/judge.py.
JUDGE_CACHE = JudgeCache()


@dataclass
class CaseResult:
    id: str
    org: str
    category: str
    should_answer: bool
    counts_toward_gate: bool

    # --- retrieval / rerank (always collected) ---
    candidate_count: int = 0
    top_rerank_score: float | None = None
    top_vector_similarity: float | None = None
    used_reranker_fallback: bool = False
    retrieved_documents: list[str] = field(default_factory=list)
    retrieved_pages: list[int] = field(default_factory=list)
    retrieval_hit: bool | None = None
    rewritten_question: str | None = None

    # --- ranked lists, IN RANK ORDER (the sets above lose the order) ---
    # Two of them, because they answer different questions. The
    # candidate list says whether retrieval found the support at all;
    # the reranked list says whether it survived into the prompt. A
    # single list cannot distinguish "the vector leg missed it" from
    # "the cross-encoder buried it", and those have different fixes.
    candidate_ranking: list[dict] = field(default_factory=list)
    reranked_ranking: list[dict] = field(default_factory=list)

    # --- retrieval metrics (None when the case labels no support) ---
    recall_at_k: float | None = None
    reciprocal_rank: float | None = None
    first_relevant_rank: int | None = None
    candidate_recall: float | None = None
    candidate_first_rank: int | None = None
    citation_pages_ok: bool | None = None

    # --- generation (only where an answer is scored) ---
    answered: bool | None = None
    answer: str | None = None
    citations: list[dict] = field(default_factory=list)
    phrases_ok: bool | None = None
    rubric_ok: bool | None = None
    pages_ok: bool | None = None
    leak_detected: bool | None = None
    notes: list[str] = field(default_factory=list)

    # --- cache probe ---
    cache_hit_second_time: bool | None = None
    cache_answer_identical: bool | None = None

    # --- step 2: what the LLM does with the gate FORCED OPEN ---
    # Collected for every should-refuse case, including those the gate
    # blocks. Without it, a case the gate catches tells us nothing about
    # whether the model would also have refused -- and that is the whole
    # question when deciding if a sufficiency check is needed.
    forced_answer: str | None = None
    forced_verdict: str | None = None
    forced_verdict_reason: str | None = None
    forced_verdict_deterministic: bool | None = None
    forced_verdict_cached: bool | None = None
    forced_verdict_model_calls: int = 0
    context_sent: str | None = None

    error: str | None = None


def log(message: str = "") -> None:
    print(message, flush=True)


class ThrottledReranker(Reranker):
    """Spaces rerank calls to stay under a trial key's per-minute cap.

    Wraps rather than replaces, so the real HostedReranker -- its wire
    format, its typed errors, its breaker -- is still what gets
    exercised. Only the timing changes.

    This lives in the EVAL, not in app/. Production deliberately has no
    reranker throttle: inside a request, waiting 6s to avoid a 429 blows
    a 3-second budget, and falling back to RRF order in milliseconds is
    strictly better. A batch job has the opposite trade -- wall-clock is
    free and a 429 would corrupt the measurement.
    """

    def __init__(self, inner: Reranker, min_interval: float):
        self._inner = inner
        self._min_interval = min_interval
        self._last: float | None = None
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return f"throttled:{self._inner.name}"

    @property
    def scores_are_calibrated(self) -> bool:
        return self._inner.scores_are_calibrated

    @property
    def is_disabled(self) -> bool:
        return getattr(self._inner, "is_disabled", False)

    async def rerank(self, query, candidates, top_n):
        # The lock makes the spacing real rather than advisory: without
        # it, two coroutines both read the same `_last` and both decide
        # they may proceed.
        async with self._lock:
            loop = asyncio.get_running_loop()
            if self._last is not None:
                wait = self._min_interval - (loop.time() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last = asyncio.get_running_loop().time()
        return await self._inner.rerank(query, candidates, top_n)


_RERANKER: Reranker | None = None


def eval_reranker() -> Reranker:
    """One throttled reranker shared by every call in the run."""
    global _RERANKER
    if _RERANKER is None:
        _RERANKER = ThrottledReranker(get_reranker(), RERANK_MIN_INTERVAL)
    return _RERANKER


# ---------------------------------------------------------------- setup ----


def reset_eval_orgs(db, golden) -> dict[str, dict]:
    """Rebuild the eval tenants from scratch, every run.

    Deleted and recreated rather than reused: a calibration run that
    inherits chunks from a previous corpus is measuring a corpus nobody
    can see. Names are prefixed so this can never touch a real tenant.
    """
    handles: dict[str, dict] = {}
    for org_name in golden.orgs():
        full_name = f"eval-{org_name}"
        existing = db.query(Organization).filter(Organization.name == full_name).first()
        if existing is not None:
            db.execute(
                sql_text("DELETE FROM document_chunks WHERE org_id = :o"),
                {"o": str(existing.id)},
            )
            db.execute(
                sql_text("DELETE FROM messages WHERE org_id = :o"),
                {"o": str(existing.id)},
            )
            db.execute(
                sql_text("DELETE FROM conversations WHERE org_id = :o"),
                {"o": str(existing.id)},
            )
            db.execute(
                sql_text("DELETE FROM documents WHERE org_id = :o"),
                {"o": str(existing.id)},
            )
            db.execute(
                sql_text("DELETE FROM users WHERE org_id = :o"), {"o": str(existing.id)}
            )
            db.delete(existing)
            db.commit()

        org_id = uuid.uuid4()
        org = Organization(id=org_id, name=full_name)
        db.add(org)
        user = User(
            id=uuid.uuid4(),
            org_id=org_id,
            email=f"eval@{org_name}.example",
            hashed_password=hash_password("eval-only-not-a-real-account"),
            role=UserRole.ADMIN,
        )
        db.add(user)
        db.commit()
        # Primitives, not ORM instances: these are read across dozens of
        # later commits, and a committed instance has expired attributes
        # (and, once expunged, no session at all). Holding the objects
        # cost three cases a DetachedInstanceError on the first run --
        # and because those cases then "retrieved nothing", the isolation
        # section reported them CLEAN. A crash that reads as a pass is
        # the worst possible failure for a security check.
        handles[org_name] = {
            "org_id": org_id,
            "user_id": user.id,
            "index_version": org.index_version,
        }
        log(f"  {full_name}: org {org_id}")
    return handles


def ingest(db, golden, handles) -> int:
    """Chunk and embed the corpus with the LIVE embedder.

    Calls the pipeline's own extract -> clean -> chunk path rather than
    the Celery task, because the task also does storage and status
    bookkeeping that the eval has no use for. The embedding provider is
    the real one, so vectors in the table are what production would
    store.
    """
    embedder = get_embedding_provider()
    log(f"  embedder: {embedder.model} ({embedder.dimensions}d)")
    total_children = 0

    for org_name in golden.orgs():
        org_id = handles[org_name]["org_id"]
        user_id = handles[org_name]["user_id"]
        for document in golden.documents_for(org_name):
            with open(document.path, "rb") as handle:
                data = handle.read()
            pages = clean_pages(extract_pages(data, document.filename))
            parents = chunk_pages(pages)

            row = Document(
                id=uuid.uuid4(),
                org_id=org_id,
                uploaded_by=user_id,
                filename=document.filename,
                storage_key=f"eval/{org_name}/{document.filename}",
                file_size_bytes=len(data),
                status=DocumentStatus.PROCESSING,
            )
            db.add(row)
            db.flush()

            # Embed everything first, then write once -- the same order
            # PART 4 moved ingestion to, so a slow provider does not hold
            # a write transaction open across network calls.
            children = [c for p in parents for c in p.children]
            vectors = embedder.embed([c.content for c in children])

            from app.models.document_chunk import DocumentChunk

            parent_ids = {}
            for parent in parents:
                parent_id = uuid.uuid4()
                parent_ids[id(parent)] = parent_id
                db.add(
                    DocumentChunk(
                        id=parent_id,
                        org_id=org_id,
                        document_id=row.id,
                        parent_id=None,
                        chunk_index=parent.chunk_index,
                        content=parent.content,
                        token_count=parent.token_count,
                        page_number=parent.page_number,
                        embedding=None,
                        embedding_model=None,
                    )
                )
            index = 0
            for parent in parents:
                for child in parent.children:
                    db.add(
                        DocumentChunk(
                            id=uuid.uuid4(),
                            org_id=org_id,
                            document_id=row.id,
                            parent_id=parent_ids[id(parent)],
                            chunk_index=child.chunk_index,
                            content=child.content,
                            token_count=child.token_count,
                            page_number=child.page_number,
                            embedding=vectors[index],
                            embedding_model=embedder.model,
                        )
                    )
                    index += 1
            total_children += len(children)
            row.status = DocumentStatus.READY
            db.commit()
            log(
                f"  {org_name}/{document.filename}: "
                f"{len(parents)} parents, {len(children)} children"
            )
    return total_children


# ------------------------------------------------------------ one case ----


def measure_similarity(db, org_id, query_vector, candidates) -> float | None:
    """Cosine similarity of the best candidate, straight from pgvector.

    `1 - (embedding <=> query)` is cosine similarity for
    vector_cosine_ops. Asked of the database rather than computed in
    Python so the number is the one the index actually ordered by.
    """
    if not candidates:
        return None
    ids = [str(c.chunk_id) for c in candidates]
    row = db.execute(
        sql_text(
            """
            SELECT MAX(1 - (embedding <=> CAST(:q AS vector))) AS sim
            FROM document_chunks
            WHERE org_id = CAST(:o AS uuid)
              AND id = ANY(CAST(:ids AS uuid[]))
              AND embedding IS NOT NULL
            """
        ),
        {"q": str(list(query_vector)), "o": str(org_id), "ids": ids},
    ).first()
    return float(row.sim) if row and row.sim is not None else None


def _ranked_items(rows) -> list[RankedItem]:
    """Children in rank order -> RankedItem, preserving the order.

    Takes anything with `.filename` / `.page_number` / `.chunk_id`,
    which covers a `Candidate` (pre-rerank) and a `RankedCandidate`'s
    `.candidate` (post-rerank). The order is the whole point: the
    existing `retrieved_documents` field is a sorted SET, and a sorted
    set cannot tell rank 1 from rank 47.
    """
    items = []
    for row in rows:
        candidate = getattr(row, "candidate", row)
        items.append(
            RankedItem(
                document=candidate.filename,
                page=candidate.page_number,
                chunk_id=str(candidate.chunk_id),
            )
        )
    return items


def _measure_retrieval(result, question, candidates, ranked) -> None:
    """Fill in recall@k, MRR and the two rank positions.

    k is `RERANK_TOP_N`, because that is how many parents are actually
    sent to the model. Measuring recall at 50 would report that the
    support was "retrieved" when the prompt never contained it.

    Both lists are measured. When candidate recall is 1.0 and reranked
    recall is 0.0, the cross-encoder demoted the right chunk -- a
    reranker problem. When both are 0.0, neither leg found it -- an
    embedding, chunking or query problem. One number cannot say which.
    """
    target = RetrievalTarget(
        documents=tuple(question.expect_documents),
        pages=tuple(question.expect_pages),
    )

    result.candidate_ranking = [
        {"document": i.document, "page": i.page, "chunk_id": i.chunk_id}
        for i in _ranked_items(candidates)
    ]
    result.reranked_ranking = [
        {"document": i.document, "page": i.page, "chunk_id": i.chunk_id}
        for i in _ranked_items(ranked)
    ]

    reranked_items = _ranked_items(ranked)
    candidate_items = _ranked_items(candidates)

    k = settings.RERANK_TOP_N
    result.recall_at_k = recall_at_k(reranked_items, target, k=k)
    result.reciprocal_rank = reciprocal_rank(reranked_items, target)
    result.first_relevant_rank = first_relevant_rank(reranked_items, target)
    # No k cap on the candidate list: the question it answers is "did
    # either retrieval leg see it at all", and capping would fold a
    # ranking question back into a retrieval one.
    result.candidate_recall = recall_at_k(
        candidate_items, target, k=len(candidate_items) or 1
    )
    result.candidate_first_rank = first_relevant_rank(candidate_items, target)


async def run_case(db, golden, handles, question, *, generate: bool) -> CaseResult:
    org_id = handles[question.org]["org_id"]

    result = CaseResult(
        id=question.id,
        org=question.org,
        category=question.category,
        should_answer=question.should_answer,
        counts_toward_gate=question.counts_toward_gate,
    )

    embedder = get_embedding_provider()
    reranker = eval_reranker()

    try:
        # --- phase 1 pieces, run explicitly so the scores are visible ---
        rewritten = question.question
        if question.history:
            from app.services.query_rewrite import rewrite_question

            rewritten = await rewrite_question(
                question.question, list(question.history), llm=get_rewrite_llm()
            )
            result.rewritten_question = rewritten

        [query_vector] = embedder.embed([rewritten], task=EmbeddingTask.QUERY)

        candidates = retrieve_candidates(
            db, org_id=org_id, query_text=rewritten, query_embedding=query_vector
        )
        result.candidate_count = len(candidates)
        result.top_vector_similarity = measure_similarity(
            db, org_id, query_vector, candidates
        )

        ranked, used_fallback = await rerank_with_fallback(
            reranker, rewritten, candidates, timeout=RERANK_TIMEOUT
        )
        result.used_reranker_fallback = used_fallback
        if ranked:
            result.top_rerank_score = float(ranked[0].score)

        parents = map_to_parents(db, ranked, org_id=org_id)
        result.retrieved_documents = sorted({p.filename for p in parents})
        result.retrieved_pages = sorted({p.page_number for p in parents})

        # Measured on the CHILD rankings, not on the parents: children
        # are what the vector index and the cross-encoder actually
        # ranked, and parent promotion deduplicates, so a parent list
        # has already collapsed the ranks the metric is about.
        _measure_retrieval(result, question, candidates, ranked)

        if question.expect_documents:
            result.retrieval_hit = any(
                name in result.retrieved_documents for name in question.expect_documents
            )
        elif question.should_answer is False:
            # Nothing was supposed to support it; "hit" is meaningless.
            result.retrieval_hit = None

        # --- generation, only where an answer gets scored ---
        # Every case now generates. Step 2 needs the FINAL ANSWER for
        # should-refuse questions too: "the gate caught it" and "the
        # system declined" are different claims, and only the second is
        # what a user experiences.
        wants_generation = generate
        if wants_generation:
            answer, citations, context = await generate_once(
                db, golden, handles, question, rewritten
            )
            result.context_sent = context
            result.answer = answer
            result.citations = citations
            result.answered = bool(answer) and not answer.startswith(
                "I could not find anything"
            )

            if question.rubric:
                rubric = check_rubric(answer, question.rubric)
                result.rubric_ok = rubric.passed
                result.notes.extend(rubric.notes)
                if rubric.missing:
                    result.notes.append(f"rubric missing: {rubric.missing}")
                if rubric.forbidden_found:
                    result.notes.append(f"rubric asserted: {rubric.forbidden_found}")
            if question.expect_phrases or question.forbid_phrases:
                phrases = check_phrases(
                    answer,
                    expect_phrases=question.expect_phrases,
                    forbid_phrases=question.forbid_phrases,
                )
                result.phrases_ok = phrases.passed
                if phrases.missing:
                    result.notes.append(f"missing phrases: {phrases.missing}")
                if phrases.forbidden_found:
                    result.notes.append(f"FORBIDDEN: {phrases.forbidden_found}")
            if question.expect_pages:
                pages = check_citation_pages(citations, question.expect_pages)
                result.pages_ok = pages.passed
                result.notes.extend(pages.notes)
            # Same question, computed from the citation pages alone so
            # the aggregate in the retrieval table and the per-case
            # check above cannot drift apart.
            result.citation_pages_ok = citation_pages_correct(
                [c.get("page") for c in citations], question.expect_pages
            )

            if question.is_isolation_case:
                leak = check_phrases(answer, forbid_phrases=question.forbid_phrases)
                result.leak_detected = not leak.passed
                # Content from the other tenant must not appear in a
                # citation either, not just in the prose.
                for citation in citations:
                    if citation.get("document") not in [
                        d.filename for d in golden.documents_for(question.org)
                    ]:
                        result.leak_detected = True
                        result.notes.append(
                            f"CITATION LEAK: {citation.get('document')}"
                        )

            # --- step 2: the same question with the gate FORCED OPEN ---
            if not question.should_answer:
                forced, forced_context = await generate_with_gate_open(
                    db, golden, handles, question
                )
                result.forced_answer = forced
                verdict = await judge_answer(
                    question=question.question,
                    context=forced_context or result.context_sent or "",
                    answer=forced,
                    # The JUDGE model, not the answering one. Same name
                    # by default and a different factory on purpose:
                    # pointing JUDGE_MODEL elsewhere must not require a
                    # code change.
                    llm=get_judge_llm(),
                    cache=JUDGE_CACHE,
                )
                result.forced_verdict = verdict.verdict
                result.forced_verdict_reason = verdict.reason
                result.forced_verdict_deterministic = verdict.deterministic
                result.forced_verdict_cached = verdict.cached
                result.forced_verdict_model_calls = verdict.model_calls

    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


async def generate_with_gate_open(db, golden, handles, question):
    """Answer a should-refuse question with the confidence gate disabled.

    The gate is bypassed by lowering CONFIDENCE_THRESHOLD for the call
    rather than by reconstructing the prompt by hand: the point is to
    measure the REAL answering path on this context, and a hand-built
    prompt would measure a prompt that never ships.

    Why this matters for the decision in front of us: if the model
    declines these unaided, a sufficiency check is redundant work on
    every request. If it answers them confidently, the gate is the only
    thing standing between a near-miss and a fabricated policy -- and the
    gate provably cannot catch all of them, because two of these score
    above every answerable case.
    """
    original = settings.CONFIDENCE_THRESHOLD
    # Below any possible score, so should_answer always opens.
    settings.CONFIDENCE_THRESHOLD = -1.0
    try:
        answer, _citations, context = await generate_once(
            db, golden, handles, question, question.question
        )
        return answer, context
    finally:
        settings.CONFIDENCE_THRESHOLD = original


async def generate_once(db, golden, handles, question, rewritten):
    """Run phases 1+2 through query_service, as the route does."""
    org_id = handles[question.org]["org_id"]
    user_id = handles[question.org]["user_id"]
    index_version = handles[question.org]["index_version"]

    conversation = Conversation(
        id=uuid.uuid4(), org_id=org_id, user_id=user_id, title=question.id
    )
    db.add(conversation)
    db.commit()

    for turn in question.history:
        db.add(
            Message(
                id=uuid.uuid4(),
                org_id=org_id,
                conversation_id=conversation.id,
                role=MessageRole(turn["role"]),
                content=turn["content"],
                kind=MessageKind(turn["kind"]) if turn.get("kind") else None,
            )
        )
    db.commit()

    cache = _CACHES.setdefault(question.id, InMemoryAnswerCache())

    async def run_blocking(fn):
        return fn()

    prepared = await query_service.prepare_query(
        session_factory=lambda: db,
        org_id=org_id,
        user_id=user_id,
        conversation_id=conversation.id,
        question=question.question,
        history=[
            {"role": t["role"], "content": t["content"]}
            for t in question.history
            if t.get("kind") not in ("refusal", "error")
        ],
        index_version=index_version,
        embedder=get_embedding_provider(),
        reranker=eval_reranker(),
        rewrite_llm=get_rewrite_llm(),
        cache=cache,
        run_blocking=run_blocking,
    )

    pieces: list[str] = []
    outcome = None
    llm = get_llm()
    async for kind, payload in query_service.generate_answer(prepared, llm=llm):
        if kind == "token":
            pieces.append(payload)
        else:
            outcome = payload

    # The ROUTE caches, not generate_answer -- so a harness that stops at
    # generate_answer never populates the cache and every second ask is a
    # MISS. That is exactly what the first run reported, and it was the
    # harness being wrong rather than the cache. should_cache() still gets
    # the final say: a gated refusal and an errored turn are not cached.
    if outcome is not None:
        await query_service.maybe_cache_answer(
            prepared,
            answer=outcome.answer,
            citations=outcome.citations,
            cache=cache,
            model=llm.model,
            errored=outcome.errored,
        )

    citations = [
        {
            "marker": c.marker,
            "document": c.document,
            "page": c.page,
        }
        for c in (outcome.citations if outcome else [])
    ]
    # The context blocks the answerer actually saw. The judge grades
    # against THIS, not against the corpus: an answer is ungrounded if it
    # claims something the retrieved context does not state, even when
    # the corpus says it elsewhere.
    context = prepared.prompt.user if prepared.prompt is not None else ""

    # The AUTHORITATIVE answer, not the raw stream. validate_citations
    # may rewrite marker placement, and the validated text is what the
    # route's final `citations` event sends, what the Message row
    # stores, and what the cache replays. Scoring the raw stream meant
    # scoring text the system had already superseded -- which showed up
    # as a cache "hit" whose answer looked DIFFERENT, when the only
    # difference was that ask 1 was compared against pre-validation text.
    answer = outcome.answer if outcome is not None else "".join(pieces)
    return answer, citations, context


_CACHES: dict[str, InMemoryAnswerCache] = {}

# Set from the CLI before any case runs.
RERANK_TIMEOUT = DEFAULT_RERANK_TIMEOUT
RERANK_MIN_INTERVAL = DEFAULT_RERANK_MIN_INTERVAL


async def probe_cache(db, golden, handles, question) -> tuple[bool, bool, str]:
    """Ask the same question a second time; it must be a cache hit.

    Uses the SAME cache instance as the first ask, keyed per question id,
    so this measures the cache key (org | normalized rewritten question |
    index_version) rather than a fresh dictionary.
    """
    org_id = handles[question.org]["org_id"]
    user_id = handles[question.org]["user_id"]
    index_version = handles[question.org]["index_version"]

    conversation = Conversation(
        id=uuid.uuid4(), org_id=org_id, user_id=user_id, title=f"{question.id}-again"
    )
    db.add(conversation)
    db.commit()

    cache = _CACHES.setdefault(question.id, InMemoryAnswerCache())

    async def run_blocking(fn):
        return fn()

    prepared = await query_service.prepare_query(
        session_factory=lambda: db,
        org_id=org_id,
        user_id=user_id,
        conversation_id=conversation.id,
        question=question.question,
        history=[
            {"role": t["role"], "content": t["content"]}
            for t in question.history
            if t.get("kind") not in ("refusal", "error")
        ],
        index_version=index_version,
        embedder=get_embedding_provider(),
        reranker=eval_reranker(),
        rewrite_llm=get_rewrite_llm(),
        cache=cache,
        run_blocking=run_blocking,
    )

    pieces: list[str] = []
    async for kind, payload in query_service.generate_answer(prepared, llm=get_llm()):
        if kind == "token":
            pieces.append(payload)
    return prepared.cache_hit, "".join(pieces), prepared.rewritten_question


# ------------------------------------------------------------- curves ----


def sweep(results, score_attr, thresholds):
    """Gate precision/recall as a function of a threshold.

    The gate ANSWERS when score >= threshold. A case with no score at
    all (nothing retrieved) is a refusal, which is the gate's own
    behaviour -- `should_answer` returns False on an empty ranking.
    """
    rows = []
    scored = [r for r in results if r.counts_toward_gate and r.error is None]

    for threshold in thresholds:
        tp = fp = tn = fn = 0
        for r in scored:
            score = getattr(r, score_attr)
            answers = score is not None and score >= threshold
            if r.should_answer and answers:
                tp += 1
            elif r.should_answer and not answers:
                fn += 1
            elif not r.should_answer and answers:
                fp += 1
            else:
                tn += 1
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision and recall
            else 0.0
        )
        rows.append(
            {
                "threshold": threshold,
                "tp": tp,
                "fp": fp,
                "tn": tn,
                "fn": fn,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "accuracy": (tp + tn) / len(scored) if scored else None,
            }
        )
    return rows


def print_curve(title, rows, note=""):
    log()
    log(f"--- {title} ---")
    if note:
        log(f"    {note}")
    log(
        f"    {'thresh':>7} {'TP':>3} {'FP':>3} {'TN':>3} {'FN':>3} "
        f"{'prec':>6} {'recall':>6} {'F1':>6} {'acc':>6}"
    )
    best = max(rows, key=lambda r: r["f1"]) if rows else None
    for row in rows:
        marker = " <= best F1" if row is best else ""
        prec = f"{row['precision']:.2f}" if row["precision"] is not None else "  -  "
        rec = f"{row['recall']:.2f}" if row["recall"] is not None else "  -  "
        acc = f"{row['accuracy']:.2f}" if row["accuracy"] is not None else "  -  "
        log(
            f"    {row['threshold']:>7.2f} {row['tp']:>3} {row['fp']:>3} "
            f"{row['tn']:>3} {row['fn']:>3} {prec:>6} {rec:>6} "
            f"{row['f1']:>6.2f} {acc:>6}{marker}"
        )
    return best


def score_table(title, results, attr):
    log()
    log(f"--- {title} ---")
    answerable = [
        getattr(r, attr) for r in results
        if r.should_answer and r.counts_toward_gate and getattr(r, attr) is not None
    ]
    refusable = [
        getattr(r, attr) for r in results
        if not r.should_answer and r.counts_toward_gate and getattr(r, attr) is not None
    ]
    for label, values in (("should ANSWER", answerable), ("should REFUSE", refusable)):
        if not values:
            log(f"    {label}: no values")
            continue
        log(
            f"    {label}: n={len(values)} "
            f"min={min(values):.4f} median={statistics.median(values):.4f} "
            f"max={max(values):.4f}"
        )
    if answerable and refusable:
        # The separation is what makes a threshold possible at all. If
        # the worst answerable sits below the best refusable, NO single
        # threshold can separate them and the gate cannot be fixed by
        # tuning -- only by better retrieval or a different signal.
        gap = min(answerable) - max(refusable)
        log(
            f"    separation: worst answerable {min(answerable):.4f} - "
            f"best refusable {max(refusable):.4f} = {gap:+.4f}"
        )
        if gap <= 0:
            log("    ** OVERLAP: no single threshold separates these classes **")
    return answerable, refusable


# --------------------------------------------------------------- main ----


async def main_async(args) -> int:
    global RERANK_TIMEOUT, RERANK_MIN_INTERVAL
    RERANK_TIMEOUT = args.rerank_timeout
    RERANK_MIN_INTERVAL = args.rerank_interval
    # Also raised on the client itself: rerank_with_fallback's wait_for
    # bounds the operation, but HostedReranker hands its own value to
    # httpx per request, and the smaller of the two wins.
    settings.RERANKER_TIMEOUT_SECONDS = args.rerank_timeout

    golden = load_golden_set()
    log(summarise(golden))

    # Captured BEFORE the first case, and before anything else mutates
    # Settings. `settings.RERANKER_TIMEOUT_SECONDS` was already raised
    # to the eval's budget above, which is exactly what must be
    # recorded -- a snapshot taken at the end would show production's
    # 3s and silently misdescribe every score in the file.
    snapshot = config_snapshot(golden=golden, settings=settings, args=args)

    log()
    log("=== providers ===")
    log(f"  embedding: {settings.EMBEDDING_PROVIDER} / {settings.EMBEDDING_MODEL}")
    log(f"  llm      : {settings.LLM_PROVIDER} / {settings.LLM_MODEL}")
    log(f"  reranker : {settings.RERANKER_PROVIDER} / {settings.RERANKER_MODEL}")
    if settings.EMBEDDING_PROVIDER == "fake" or settings.RERANKER_PROVIDER != "hosted":
        log("  WARNING: not a live configuration; numbers below are meaningless")

    db = SessionLocal()
    results: list[CaseResult] = []
    try:
        log()
        log("=== tenants ===")
        handles = reset_eval_orgs(db, golden)

        log()
        log("=== ingest (live embeddings) ===")
        started = time.monotonic()
        children = ingest(db, golden, handles)
        log(f"  {children} children embedded in {time.monotonic() - started:.1f}s")

        log()
        log(f"=== cases (pacing {args.pace:.1f}s between cases) ===")
        if args.smoke:
            selected = [q for q in golden.questions if q.id in SMOKE_IDS]
            missing = sorted(set(SMOKE_IDS) - {q.id for q in selected})
            if missing:
                # A renamed case silently shrinks the smoke set, and a
                # smoke run that quietly covers six shapes instead of
                # nine is worse than one that complains.
                log(f"  WARNING: smoke ids not in the golden set: {missing}")
        else:
            selected = [
                q for q in golden.questions
                if not args.only or q.id in args.only
            ]
        for position, question in enumerate(selected):
            if position:
                time.sleep(args.pace)
            started = time.monotonic()
            result = await run_case(
                db, golden, handles, question, generate=not args.no_generate
            )
            results.append(result)

            bits = [f"{result.id:32}"]
            bits.append(
                f"rerank={result.top_rerank_score:.4f}"
                if result.top_rerank_score is not None
                else "rerank=  none "
            )
            bits.append(
                f"cos={result.top_vector_similarity:.4f}"
                if result.top_vector_similarity is not None
                else "cos=  none "
            )
            bits.append(f"want={'ANS' if result.should_answer else 'REF'}")
            if result.answered is not None:
                bits.append(f"got={'ANS' if result.answered else 'REF'}")
            if result.error:
                bits.append(f"ERROR {result.error}")
            log("  " + "  ".join(bits) + f"  ({time.monotonic() - started:.1f}s)")
            for note in result.notes:
                log(f"      note: {note}")

        # --- cache probe: ask every answered question a second time ---
        if not args.no_generate:
            log()
            log("=== cache probe (second ask must hit) ===")
            for question in selected:
                if not question.should_answer:
                    continue
                result = next(r for r in results if r.id == question.id)
                if result.answer is None or result.error:
                    continue
                time.sleep(args.pace)
                try:
                    hit, answer, _ = await probe_cache(db, golden, handles, question)
                    result.cache_hit_second_time = hit
                    result.cache_answer_identical = answer.strip() == result.answer.strip()
                    status = "HIT" if hit else "MISS"
                    same = "same" if result.cache_answer_identical else "DIFFERENT"
                    log(f"  {question.id:32} {status:4}  answer {same}")
                except Exception as exc:
                    log(f"  {question.id:32} ERROR {type(exc).__name__}: {exc}")
    finally:
        db.close()
        await aclose_async_client()

    # Written BEFORE the report, on purpose: a reporting bug must not
    # discard a run that has already paid for live API calls.
    #
    # Every run gets its own file now, including partial ones. The old
    # code skipped writing for `--only` to protect the single shared
    # results.json from being replaced by two cases -- a real hazard
    # that a timestamped filename removes outright. A partial run is
    # still data, and throwing it away to protect a filename was the
    # wrong trade.
    label = "smoke" if args.smoke else ("partial" if args.only else "full")
    snapshot["selection"] = {
        "label": label,
        "case_ids": [r.id for r in results],
        "n_selected": len(results),
        "n_in_set": len(golden.questions),
        # A partial run cannot support a threshold. Recorded in the file
        # rather than only printed, so a reader who finds the file later
        # does not have to infer it from the case count.
        "sufficient_for_calibration": label == "full",
    }
    snapshot["judge_cache"] = {
        "hits": JUDGE_CACHE.hits,
        "misses": JUDGE_CACHE.misses,
        "enabled": JUDGE_CACHE.enabled,
    }
    snapshot["api_calls"] = {
        "judge_model_calls": sum(r.forced_verdict_model_calls for r in results),
    }

    run_path = write_run(config=snapshot, cases=results, label=label)
    log()
    log(f"run written to {run_path}")
    log(f"  golden set version {snapshot['golden_set_version']}, "
        f"{len(results)} case(s), label '{label}'")
    if label != "full":
        log("  NOT a calibration run: a threshold fitted to this subset")
        log("  would be fitted to the cases that happened to be selected.")

    choices = report(golden, results, args)
    return 0


def _case_retrieval(results) -> list[CaseRetrieval]:
    return [
        CaseRetrieval(
            id=r.id,
            recall=r.recall_at_k,
            reciprocal_rank=r.reciprocal_rank,
            first_rank=r.first_relevant_rank,
            citation_pages_ok=r.citation_pages_ok,
        )
        for r in results
        if r.error is None
    ]


def _fmt(value, width=6, places=3):
    return f"{value:>{width}.{places}f}" if value is not None else f"{'-':>{width}}"


def retrieval_report(results) -> None:
    """Recall@k, MRR and citation pages -- overall, per type, per org.

    Three tables rather than one blended score. A single retrieval
    number averages a shape that always works with one that never does,
    and the whole reason the four retrieval-shape types were added to
    the golden set was so that "every table case fails while the prose
    cases pass" is a visible row rather than a dip in an average.

    k is RERANK_TOP_N: the number of parents actually sent to the
    model. Recall measured at the candidate count would report support
    as found when the prompt never contained it.
    """
    k = settings.RERANK_TOP_N
    log()
    log("--- RETRIEVAL METRICS (recall@k, MRR) ---")
    log(f"    k = RERANK_TOP_N = {k} (what the prompt actually receives)")
    log("    recall is the fraction of LABELLED (document, page) units found;")
    log("    MRR is over the first relevant rank. Cases labelling no support")
    log("    (off-topic, isolation probes) are excluded, not scored zero.")

    usable = [r for r in results if r.error is None]
    overall = summarise_retrieval(_case_retrieval(usable), k=k)
    log()
    log(f"    overall       n={overall.n:3} scored={overall.n_scored:3}  "
        f"recall@{k}={_fmt(overall.recall_at_k)}  MRR={_fmt(overall.mrr)}  "
        f"cited-page={_fmt(overall.citation_pages_ok)} "
        f"(n={overall.n_citation_scored})")

    for heading, key in (("per type", "category"), ("per org", "org")):
        log()
        log(f"    {heading}")
        log(f"      {'bucket':24} {'n':>3} {'scored':>6}  {'recall':>6}  "
            f"{'MRR':>6}  {'pages':>6}")
        for bucket in sorted({getattr(r, key) for r in usable}):
            rows = [r for r in usable if getattr(r, key) == bucket]
            summary = summarise_retrieval(_case_retrieval(rows), k=k)
            flag = "  << small n, do not conclude" if summary.small_n else ""
            log(
                f"      {bucket:24} {summary.n:>3} {summary.n_scored:>6}  "
                f"{_fmt(summary.recall_at_k)}  {_fmt(summary.mrr)}  "
                f"{_fmt(summary.citation_pages_ok)}{flag}"
            )

    # A miss and a below-k are different failures with different fixes,
    # so they are listed apart rather than counted together.
    if overall.missed:
        log()
        log("    NEVER RETRIEVED (no leg found the support at any rank):")
        for case_id in overall.missed:
            row = next(r for r in usable if r.id == case_id)
            log(f"      {case_id:34} candidate_rank="
                f"{row.candidate_first_rank or 'none'}")
    if overall.below_k:
        log()
        log(f"    RETRIEVED BUT BELOW k={k} (the model never saw it):")
        for case_id in overall.below_k:
            row = next(r for r in usable if r.id == case_id)
            log(f"      {case_id:34} rank={row.first_relevant_rank}")

    # The diagnostic split the two ranked lists exist for.
    demoted = [
        r for r in usable
        if r.candidate_recall and r.candidate_recall > 0
        and (r.recall_at_k or 0) < r.candidate_recall
    ]
    if demoted:
        log()
        log("    DEMOTED BY THE RERANKER (retrieval found it, ranking lost it):")
        log("      a reranker problem, not an embedding or chunking one")
        for row in demoted:
            log(
                f"      {row.id:34} candidate_rank={row.candidate_first_rank} "
                f"-> reranked_rank={row.first_relevant_rank or 'none'}"
            )


def judge_agreement_report(results) -> None:
    """How often the judge matched a HAND grade.

    An unmeasured judge is an opinion. The rule is the skill's: at least
    10 hand grades covering BOTH refusals and assertions, and below 80%
    the judge prompt gets fixed before anything downstream of it is
    believed.
    """
    log()
    log("--- JUDGE RELIABILITY ---")
    judged = {
        r.id: r.forced_verdict
        for r in results
        if r.forced_verdict is not None
        # Deterministic verdicts (the system's own refusal text) are
        # excluded: they are a string match, not a judgement, and
        # counting them would inflate agreement with cases no model
        # ever looked at.
        and not r.forced_verdict_deterministic
    }
    hand = load_hand_grades()
    if not hand:
        log("    agreement UNMEASURED -- no hand grades in "
            "eval/golden/judge_grades.yaml")
        log("    Write at least 10 by hand, including cases the judge PASSED")
        log("    and cases it FAILED, then re-run. Until then every number")
        log("    derived from the judge is unvalidated.")
        return

    agreement = score_agreement(judged, hand)
    rate = agreement.rate
    log(f"    {agreement.agreed}/{agreement.n} hand grades matched"
        + (f" ({rate * 100:.0f}%)" if rate is not None else ""))
    if agreement.meets_bar is None:
        log(f"    VERDICT: UNKNOWN -- {agreement.n} grades is fewer than 10.")
        log("    Not 'the judge is unreliable': there is not enough evidence")
        log("    to say either way.")
    elif agreement.meets_bar:
        log("    VERDICT: at or above the 80% bar.")
    else:
        log("    VERDICT: BELOW the 80% bar. Fix the judge prompt before")
        log("    trusting the refusal measurement above.")
    if not agreement.covers_both_outcomes:
        log("    WARNING: the hand grades cover only one kind of outcome, so")
        log("    this rate says nothing about the other. Grade some cases")
        log("    where the judge said the answer ASSERTED something.")
    for row in agreement.disagreements:
        log(f"    DISAGREED {row['id']:30} judge={row['judge']:12} "
            f"hand={row['hand']}")
    if agreement.unmatched_grades:
        log(f"    hand grades with no matching case (renamed?): "
            f"{agreement.unmatched_grades}")

    cached = sum(1 for r in results if r.forced_verdict_cached)
    calls = sum(r.forced_verdict_model_calls for r in results)
    log(f"    judge cost this run: {calls} model call(s), {cached} served "
        f"from cache")


def report(golden, results, args):
    log()
    log("================ RESULTS ================")

    fell_back = [
        r for r in results if r.counts_toward_gate and r.used_reranker_fallback
    ]
    if fell_back:
        log()
        log(f"--- !! {len(fell_back)} gate-scored case(s) FELL BACK to RRF !! ---")
        log("    Their 'reranker score' is an RRF score (~0.016), not a relevance.")
        log("    Curve A below is invalid for them -- re-run with a larger")
        log("    --rerank-timeout or slower --pace before trusting it.")
        for r in fell_back:
            log(f"    {r.id}")

    errors = [r for r in results if r.error]
    if errors:
        log()
        log(f"--- {len(errors)} case(s) errored ---")
        for r in errors:
            log(f"    {r.id}: {r.error}")

    # ---- retrieval ----
    log()
    log("--- retrieval (did the supporting document come back?) ---")
    with_expectation = [r for r in results if r.retrieval_hit is not None]
    hits = sum(1 for r in with_expectation if r.retrieval_hit)
    log(f"    {hits}/{len(with_expectation)} cases retrieved an expected document")
    for r in with_expectation:
        if not r.retrieval_hit:
            log(f"    MISS {r.id}: got {r.retrieved_documents or 'nothing'}")

    # ---- citation pages ----
    page_checked = [r for r in results if r.pages_ok is not None]
    if page_checked:
        ok = sum(1 for r in page_checked if r.pages_ok)
        log()
        log("--- citation pages (PDF) ---")
        log(f"    {ok}/{len(page_checked)} cited the expected page")
        for r in page_checked:
            if not r.pages_ok:
                log(f"    WRONG PAGE {r.id}: cited {r.retrieved_pages}")

    retrieval_report(results)

    # ---- score distributions and the two curves ----
    score_table("reranker score distribution (gate-scored only)", results,
                "top_rerank_score")
    score_table("vector cosine distribution (gate-scored only)", results,
                "top_vector_similarity")

    rerank_rows = sweep(
        results, "top_rerank_score", [round(x * 0.05, 2) for x in range(0, 21)]
    )
    best_rerank = print_curve(
        "CURVE A: reranker-score threshold (CONFIDENCE_THRESHOLD)",
        rerank_rows,
        note="what should_answer compares against when the cross-encoder ran",
    )

    cosine_rows = sweep(
        results, "top_vector_similarity", [round(0.40 + x * 0.025, 3) for x in range(0, 21)]
    )
    best_cosine = print_curve(
        "CURVE B: vector-similarity floor (reranker DOWN)",
        cosine_rows,
        note="today a fallback opens the gate unconditionally; this is the "
        "only calibrated signal left in that regime",
    )

    # ---- per-type breakdown ----
    log()
    log("--- per type (at the best-F1 reranker threshold) ---")
    chosen = best_rerank["threshold"] if best_rerank else settings.CONFIDENCE_THRESHOLD
    for category in sorted({r.category for r in results}):
        rows = [r for r in results if r.category == category and r.error is None]
        correct = 0
        for r in rows:
            answers = (
                r.top_rerank_score is not None and r.top_rerank_score >= chosen
            )
            if answers == r.should_answer:
                correct += 1
        tag = " (EXCLUDED from gate score)" if category in GATE_EXCLUDED else ""
        log(f"    {category:24} {correct}/{len(rows)} would be decided correctly{tag}")

    # ---- ambiguous, reported alone ----
    log()
    log("--- ambiguous cases (never counted in the curves above) ---")
    for r in results:
        if r.category != "ambiguous":
            continue
        decision = (
            "ANSWER"
            if (r.top_rerank_score is not None and r.top_rerank_score >= chosen)
            else "REFUSE"
        )
        log(
            f"    {r.id:28} rerank="
            + (f"{r.top_rerank_score:.4f}" if r.top_rerank_score is not None else "none")
            + f"  would {decision}  (labelled {'ANSWER' if r.should_answer else 'REFUSE'})"
        )

    # ---- synthesis, retrieval vs generation split ----
    log()
    log("--- synthesis cases (retrieval and generation reported separately) ---")
    for r in results:
        if r.category != "synthesis":
            continue
        log(
            f"    {r.id:28} retrieval={'HIT ' if r.retrieval_hit else 'MISS'} "
            f"pages={'ok' if r.pages_ok else 'WRONG' if r.pages_ok is not None else '-'} "
            f"generation={'PASS' if r.rubric_ok else 'FAIL' if r.rubric_ok is not None else '-'}"
        )
        if r.answer:
            log(f"      answer: {r.answer.strip()[:160]}")

    # ---- generation quality on answerable cases ----
    generated = [r for r in results if r.answer is not None and r.should_answer]
    if generated:
        log()
        log("--- generation on answerable cases ---")
        for r in generated:
            checks = []
            if r.phrases_ok is not None:
                checks.append(f"phrases={'ok' if r.phrases_ok else 'FAIL'}")
            if r.rubric_ok is not None:
                checks.append(f"rubric={'ok' if r.rubric_ok else 'FAIL'}")
            if r.pages_ok is not None:
                checks.append(f"pages={'ok' if r.pages_ok else 'FAIL'}")
            log(f"    {r.id:32} {'  '.join(checks)}")

    # ---- isolation ----
    log()
    log("--- cross-org isolation ---")
    isolation = [r for r in results if r.category == "cross_org_isolation"]
    for r in isolation:
        if r.error or r.leak_detected is None:
            # NOT "clean": the probe did not complete. A crash that reads
            # as a pass is the worst outcome for a security check.
            verdict = "UNKNOWN"
        else:
            verdict = "LEAK" if r.leak_detected else "clean"
        log(
            f"    {r.id:32} {verdict:5} "
            f"retrieved={r.retrieved_documents or 'nothing'} "
            f"answered={r.answered}"
        )
    if any(r.leak_detected for r in isolation):
        log("    ** CROSS-TENANT LEAK -- this is a security finding **")
    unverified = [r for r in isolation if r.error or r.leak_detected is None]
    if unverified:
        log(
            f"    ** {len(unverified)} isolation probe(s) did not complete; "
            f"isolation is UNVERIFIED, not passing **"
        )

    # ---- cache ----
    probed = [r for r in results if r.cache_hit_second_time is not None]
    if probed:
        log()
        log("--- cache (second ask) ---")
        hits = sum(1 for r in probed if r.cache_hit_second_time)
        same = sum(1 for r in probed if r.cache_answer_identical)
        log(f"    {hits}/{len(probed)} were cache hits")
        log(f"    {same}/{len(probed)} returned a byte-identical answer")
        for r in probed:
            if not r.cache_hit_second_time or not r.cache_answer_identical:
                log(
                    f"    PROBLEM {r.id}: hit={r.cache_hit_second_time} "
                    f"identical={r.cache_answer_identical}"
                )

    # ---- STEP 1: the full sorted lists and the fixed-rule choice ----
    def scored(attr):
        return [
            ScoredCase(
                id=r.id,
                category=r.category,
                score=getattr(r, attr),
                should_answer=r.should_answer,
            )
            for r in results
            if r.counts_toward_gate
            and r.error is None
            and getattr(r, attr) is not None
        ]

    log()
    log("================ STEP 1: THRESHOLD SELECTION ================")

    choices = {}
    for label, attr, setting in (
        ("RERANKER SCORE", "top_rerank_score", "CONFIDENCE_THRESHOLD"),
        ("VECTOR COSINE", "top_vector_similarity", "VECTOR_SIMILARITY_FLOOR"),
    ):
        cases = scored(attr)
        log()
        log(sorted_score_table(cases, f"--- {label}: every gate-scored case ---"))

        choice = select_threshold(cases)
        choices[setting] = (choice, cases)

        log()
        log(f"  rule: midpoint of the widest gap below the lowest answerable")
        log(f"        score; fall back to 0.50 if no gap reaches 0.03")
        log(f"  -> {setting} = {choice.value}")
        log(f"     {choice.reason}")
        if choice.irreducible_false_positives:
            log(
                f"     IRREDUCIBLE: {len(choice.irreducible_false_positives)} "
                f"should-refuse case(s) score at or above the lowest answerable "
                f"score, so NO threshold excludes them without losing a real "
                f"answer:"
            )
            for case_id in choice.irreducible_false_positives:
                log(f"       {case_id}")
        matrix = confusion(cases, choice.value)
        log(
            f"     at {choice.value}: TP={matrix['tp']} FP={matrix['fp']} "
            f"TN={matrix['tn']} FN={matrix['fn']} "
            f"precision={matrix['precision']} recall={matrix['recall']}"
        )

    # ---- STEP 2: end-to-end refusal, gate AND model ----
    log()
    log("================ STEP 2: END-TO-END REFUSAL ================")
    gate_threshold = choices["CONFIDENCE_THRESHOLD"][0].value

    refuse = [
        r for r in results
        if not r.should_answer and r.counts_toward_gate and r.error is None
    ]

    log()
    log("  Each should-refuse case, at the chosen threshold.")
    log("  'gate' = would the confidence gate let it through?")
    log("  'final' = what the USER ends up with end to end.")
    log("  'model alone' = what the LLM does with the gate forced open,")
    log("                  which is the only way to tell whether the gate")
    log("                  is carrying the refusal or the model is.")
    log()
    log(
        f"    {'case':34} {'score':>7}  {'gate':6} {'final':9} {'model alone':12} why"
    )

    correct_end_to_end = 0
    model_declined = 0
    for r in sorted(refuse, key=lambda x: -(x.top_rerank_score or 0)):
        passes_gate = (
            r.top_rerank_score is not None and r.top_rerank_score >= gate_threshold
        )
        # End to end: a blocked case gets the canned refusal, so it
        # declines by construction. A case that passes the gate ends up
        # with whatever the model said -- which is exactly the forced
        # answer, since the gate was the only thing in the way.
        if passes_gate:
            final_declined = r.forced_verdict == "DECLINED"
        else:
            final_declined = True
        correct_end_to_end += 1 if final_declined else 0
        model_declined += 1 if r.forced_verdict == "DECLINED" else 0

        log(
            f"    {r.id:34} {r.top_rerank_score or 0:>7.4f}  "
            f"{'PASS' if passes_gate else 'block':6} "
            f"{'declines' if final_declined else 'ASSERTS':9} "
            f"{(r.forced_verdict or '-'):12} {(r.forced_verdict_reason or '')[:60]}"
        )

    log()
    log(
        f"  end-to-end correct refusal: {correct_end_to_end}/{len(refuse)} "
        f"({correct_end_to_end / len(refuse) * 100:.0f}%)"
        if refuse else "  no should-refuse cases"
    )
    log(
        f"  model alone (gate forced open): {model_declined}/{len(refuse)} declined"
    )

    # The two cases that motivated this step.
    log()
    log("  --- the two cases no threshold can catch ---")
    for case_id in ("acme-rollback-migrations-how", "acme-deploy-friday"):
        row = next((r for r in results if r.id == case_id), None)
        if row is None:
            continue
        passes_gate = (
            row.top_rerank_score is not None
            and row.top_rerank_score >= gate_threshold
        )
        log(
            f"    {case_id}: rerank={row.top_rerank_score:.4f} "
            f"-> gate {'PASSES it through' if passes_gate else 'blocks it'}"
        )
        log(f"      model alone: {row.forced_verdict} ({row.forced_verdict_reason})")
        if row.forced_answer:
            log(f"      answer: {row.forced_answer.strip()[:220]}")

    unparseable = [r for r in refuse if r.forced_verdict == "UNPARSEABLE"]
    if unparseable:
        log()
        log(f"  WARNING: the judge returned no usable verdict for "
            f"{len(unparseable)} case(s); read them by hand:")
        for r in unparseable:
            log(f"    {r.id}")

    judge_agreement_report(results)

    log()
    log("    Nothing in app/ has been changed by this run. The chosen")
    log("    values above are written to docs/eval-baseline.md and applied")
    log("    to Settings in a separate, reviewable commit.")

    return choices


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        action="append",
        help="Question id(s) to run; repeatable. Re-verify without paying "
        "for the whole set.",
    )
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="Reuse the existing eval tenants and their embeddings.",
    )
    parser.add_argument(
        "--rerank-timeout",
        type=float,
        default=DEFAULT_RERANK_TIMEOUT,
        help="Reranker budget for the EVAL (not production's 3s user budget).",
    )
    parser.add_argument(
        "--rerank-interval",
        type=float,
        default=DEFAULT_RERANK_MIN_INTERVAL,
        help="Minimum seconds between rerank CALLS (a trial key is 10/min).",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=DEFAULT_PACE_SECONDS,
        help="Seconds between cases, to stay under a trial key's per-minute cap.",
    )
    parser.add_argument(
        "--no-generate",
        action="store_true",
        help="Collect scores and curves only; skip every model call for answers.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Nine representative cases, one per retrieval shape and gate "
        "regime. For checking the harness, never for choosing a threshold.",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

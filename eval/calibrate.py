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
from dataclasses import dataclass, field, asdict

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
from app.services.llm import get_llm, get_rewrite_llm
from app.services.reranker import get_reranker, rerank_with_fallback, map_to_parents
from app.services.retrieval import retrieve_candidates
from eval.loader import GATE_EXCLUDED, load_golden_set, summarise
from eval.scoring import check_citation_pages, check_phrases, check_rubric

RESULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")

# A Cohere TRIAL key allows 10 rerank requests per minute. 26 cases fired
# back to back would mostly 429, fall back to RRF order, and report
# `used_fallback` -- which would silently poison Curve A with the one
# regime it is not measuring. Pacing is correct HERE and wrong in the
# request path: this is a batch job where wall-clock is free, whereas a
# user's query has a 3-second budget.
DEFAULT_PACE_SECONDS = 7.0

# RERANKER_TIMEOUT_SECONDS is 3s because that is a USER's budget: past
# it, RRF order now beats waiting. The eval has the opposite need -- a
# timeout here means the case is scored on RRF instead of the
# cross-encoder, so the run would calibrate the fallback regime while
# claiming to calibrate the gate. Measured in a first pass: 3s timed out
# on roughly a third of cases.
DEFAULT_RERANK_TIMEOUT = 25.0


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

    error: str | None = None


def log(message: str = "") -> None:
    print(message, flush=True)


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
    reranker = get_reranker()

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

        if question.expect_documents:
            result.retrieval_hit = any(
                name in result.retrieved_documents for name in question.expect_documents
            )
        elif question.should_answer is False:
            # Nothing was supposed to support it; "hit" is meaningless.
            result.retrieval_hit = None

        # --- generation, only where an answer gets scored ---
        wants_generation = generate and (
            question.should_answer or question.is_isolation_case
        )
        if wants_generation:
            answer, citations = await generate_once(
                db, golden, handles, question, rewritten
            )
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

    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


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
        reranker=get_reranker(),
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
    # The AUTHORITATIVE answer, not the raw stream. validate_citations
    # may rewrite marker placement, and the validated text is what the
    # route's final `citations` event sends, what the Message row
    # stores, and what the cache replays. Scoring the raw stream meant
    # scoring text the system had already superseded -- which showed up
    # as a cache "hit" whose answer looked DIFFERENT, when the only
    # difference was that ask 1 was compared against pre-validation text.
    answer = outcome.answer if outcome is not None else "".join(pieces)
    return answer, citations


_CACHES: dict[str, InMemoryAnswerCache] = {}

# Set from --rerank-timeout before any case runs.
RERANK_TIMEOUT = DEFAULT_RERANK_TIMEOUT


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
        reranker=get_reranker(),
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
    global RERANK_TIMEOUT
    RERANK_TIMEOUT = args.rerank_timeout
    # Also raised on the client itself: rerank_with_fallback's wait_for
    # bounds the operation, but HostedReranker hands its own value to
    # httpx per request, and the smaller of the two wins.
    settings.RERANKER_TIMEOUT_SECONDS = args.rerank_timeout

    golden = load_golden_set()
    log(summarise(golden))

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

    report(golden, results, args)
    with open(RESULTS_PATH, "w", encoding="utf-8") as handle:
        json.dump([asdict(r) for r in results], handle, indent=2)
    log()
    log(f"raw results written to {RESULTS_PATH}")
    return 0


def report(golden, results, args) -> None:
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

    # ---- the proposal ----
    log()
    log("================ PROPOSAL ================")
    log(f"    current CONFIDENCE_THRESHOLD = {settings.CONFIDENCE_THRESHOLD}")
    if best_rerank:
        log(
            f"    A) reranker threshold {best_rerank['threshold']:.2f} -> "
            f"precision {best_rerank['precision']}, recall {best_rerank['recall']}, "
            f"F1 {best_rerank['f1']:.2f}"
        )
    if best_cosine:
        log(
            f"    B) vector floor      {best_cosine['threshold']:.3f} -> "
            f"precision {best_cosine['precision']}, recall {best_cosine['recall']}, "
            f"F1 {best_cosine['f1']:.2f}"
        )
    log()
    log("    Nothing has been changed. Both numbers need a human decision:")
    log("    F1 weights a wrong answer and a wrong refusal equally, and for a")
    log("    grounded-answers product they are not equal.")


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
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

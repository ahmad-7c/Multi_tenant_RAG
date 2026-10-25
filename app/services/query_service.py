"""Query orchestration.

Built around one constraint: **no DB session is held while the LLM
streams.**

A streaming answer takes seconds; a connection pool has tens of slots. If
each in-flight stream pinned one, a few dozen concurrent users would
exhaust the pool and every unrelated request -- logins, uploads, document
polling -- would block waiting on a connection held open by a model
writing prose. Worse, SQLAlchemy keeps a transaction open on that
connection, so Postgres cannot vacuum any row that transaction can still
see: a long stream becomes a long-running transaction, which is how
bloat starts.

Three phases with explicit boundaries:

    phase 1  (DB touched only inside run_blocking, each with its own
              short-lived session)
        load conversation + history + index_version
        rewrite the question           (cheap LLM; no DB)
        cache lookup                   (Redis; no DB)
        embed                          (network; no DB)
        retrieve                       (BLOCKING DB -> worker thread)
        rerank                         (network; no DB)
        confidence gate
        map to parents                 (BLOCKING DB -> worker thread)
        build the prompt               (pure)
        -> returns PreparedQuery: plain frozen data, no ORM, no Session

    phase 2  (NO session anywhere)
        stream tokens to the client
        client disconnect -> CancelledError -> upstream call cancelled

    phase 3  (a new, short session on a worker thread)
        persist the user message and the answer with its citations

Why PreparedQuery holds only plain values: an ORM instance carried into
phase 2 could lazy-load an attribute and silently reopen a connection in
the middle of streaming, defeating the whole arrangement. Copying the data
out makes that impossible rather than merely discouraged.

How the blocking work gets off the event loop: psycopg2 is a SYNCHRONOUS
driver, so every query blocks the thread it runs on. Each blocking step is
handed to `run_blocking` (by default `asyncio.to_thread`, i.e. the default
threadpool) as a self-contained callable that opens its own session, does
one unit of work, returns frozen dataclasses, and closes the session. A
Session is not thread-safe, so no session is ever shared across the
boundary -- each thread gets its own.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

from app.core.config import settings
from app.services.cache import (
    AnswerCache,
    CachedAnswer,
    answer_cache_key,
    should_cache,
)
from app.services.citations import Citation, validate_citations
from app.services.embedding import EmbeddingProvider
from app.services.llm import LLMProvider
from app.services.prompting import INSUFFICIENT_CONTEXT_ANSWER, BuiltPrompt, build_prompt
from app.services.query_rewrite import rewrite_question
from app.services.reranker import (
    NOT_FOUND_ANSWER,
    Reranker,
    map_to_parents,
    rerank_with_fallback,
    should_answer,
)
from app.services.retrieval import retrieve_candidates

logger = logging.getLogger(__name__)

# run_blocking(fn) -> awaitable of fn's result.
RunBlocking = Callable[[Callable[[], object]], Awaitable[object]]


async def _to_thread(fn):
    return await asyncio.to_thread(fn)


@dataclass
class PreparedQuery:
    org_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    raw_question: str
    rewritten_question: str
    was_rewritten: bool
    index_version: int
    cache_key: str
    prompt: BuiltPrompt | None = None
    # Set when the answer is already known and no model call is needed.
    short_circuit_answer: str | None = None
    short_circuit_citations: list[Citation] = field(default_factory=list)
    gated: bool = False
    cache_hit: bool = False
    used_reranker_fallback: bool = False
    candidate_count: int = 0
    parent_count: int = 0


async def prepare_query(
    *,
    session_factory,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    question: str,
    history: list[dict],
    index_version: int,
    embedder: EmbeddingProvider,
    reranker: Reranker,
    rewrite_llm: LLMProvider,
    cache: AnswerCache,
    run_blocking: RunBlocking = _to_thread,
) -> PreparedQuery:
    """Phase 1. Returns plain data; holds no session on return."""
    # Rewriting runs BEFORE the cache lookup because the cache is keyed on
    # the rewritten question. That means the rewrite model is paid even on
    # a cache hit -- accepted, because keying on the raw text would serve
    # one conversation's answer into another whenever two users typed the
    # same follow-up ("what about contractors?").
    rewritten = await rewrite_question(question, history, llm=rewrite_llm)

    prepared = PreparedQuery(
        org_id=org_id,
        user_id=user_id,
        conversation_id=conversation_id,
        raw_question=question,
        rewritten_question=rewritten,
        was_rewritten=rewritten != question,
        index_version=index_version,
        cache_key=answer_cache_key(
            org_id=org_id, question=rewritten, index_version=index_version
        ),
    )

    cached = await cache.get(prepared.cache_key)
    if cached is not None:
        prepared.cache_hit = True
        prepared.short_circuit_answer = cached.answer
        prepared.short_circuit_citations = [Citation(**c) for c in cached.citations]
        return prepared

    [query_vector] = embedder.embed([rewritten])

    def _retrieve():
        # Own session, opened and closed inside the worker thread: a
        # Session is not thread-safe, so it must never cross the boundary.
        db = session_factory()
        try:
            return retrieve_candidates(
                db, org_id=org_id, query_text=rewritten, query_embedding=query_vector
            )
        finally:
            db.close()

    candidates = await run_blocking(_retrieve)
    prepared.candidate_count = len(candidates)

    ranked, used_fallback = await rerank_with_fallback(reranker, rewritten, candidates)
    prepared.used_reranker_fallback = used_fallback

    if not should_answer(ranked, reranker=reranker, used_fallback=used_fallback):
        # The gate: no model call at all. Both the cost saving and the
        # hallucination prevention come from this branch.
        prepared.gated = True
        prepared.short_circuit_answer = NOT_FOUND_ANSWER
        return prepared

    def _map_parents():
        db = session_factory()
        try:
            return map_to_parents(db, ranked, org_id=org_id)
        finally:
            db.close()

    parents = await run_blocking(_map_parents)
    prepared.parent_count = len(parents)
    if not parents:
        prepared.gated = True
        prepared.short_circuit_answer = NOT_FOUND_ANSWER
        return prepared

    prepared.prompt = build_prompt(rewritten, parents)
    return prepared


async def generate_answer(prepared: PreparedQuery, *, llm: LLMProvider):
    """Phase 2. Yields ("token", str) then ("done", (answer, citations, errored)).

    No database session is open while this runs.
    """
    if prepared.short_circuit_answer is not None:
        # A cache hit or a gated refusal. Emitted as a single token event
        # so the client parses one format either way.
        yield "token", prepared.short_circuit_answer
        yield "done", (
            prepared.short_circuit_answer,
            prepared.short_circuit_citations,
            False,
        )
        return

    assert prepared.prompt is not None
    pieces: list[str] = []
    errored = False
    try:
        async for piece in llm.stream(
            system=prepared.prompt.system,
            user=prepared.prompt.user,
            max_tokens=settings.LLM_MAX_TOKENS,
            temperature=settings.LLM_TEMPERATURE,
        ):
            pieces.append(piece)
            yield "token", piece
    except asyncio.CancelledError:
        # The client disconnected. Re-raised rather than swallowed so the
        # cancellation propagates into the provider's own coroutine and the
        # upstream request is actually aborted -- otherwise we keep paying
        # for tokens nobody will read.
        logger.info("answer stream cancelled; aborting the upstream LLM call")
        raise
    except Exception:
        logger.warning("LLM stream failed", exc_info=True)
        errored = True

    raw_answer = "".join(pieces)
    if errored and not raw_answer:
        yield "done", (
            "Something went wrong generating an answer. Please try again.",
            [],
            True,
        )
        return

    # Validation runs after streaming on pure text, which is why
    # citations.py needs no database and can run with the session closed.
    answer, citations = validate_citations(raw_answer, prepared.prompt.blocks)
    if not answer.strip():
        answer = INSUFFICIENT_CONTEXT_ANSWER
    yield "done", (answer, citations, errored)


async def maybe_cache_answer(
    prepared: PreparedQuery,
    *,
    answer: str,
    citations: list[Citation],
    cache: AnswerCache,
    model: str,
    errored: bool,
) -> None:
    if prepared.cache_hit:
        return
    if not should_cache(
        answer=answer, citations=citations, gated=prepared.gated, errored=errored
    ):
        return
    await cache.set(
        prepared.cache_key,
        CachedAnswer(
            answer=answer,
            # Serialised to JSON, so the UUIDs have to become strings.
            citations=[
                {
                    **asdict(c),
                    "document_id": str(c.document_id),
                    "chunk_id": str(c.chunk_id),
                }
                for c in citations
            ],
            model=model,
            cached_at=time.time(),
        ),
        settings.CACHE_TTL_SECONDS,
    )


def persist_turn(db, *, prepared: PreparedQuery, answer: str, citations: list[Citation]) -> None:
    """Phase 3. Called with a fresh session, only once streaming is done."""
    from app.models.conversation import Message, MessageRole

    db.add(
        Message(
            id=uuid.uuid4(),
            conversation_id=prepared.conversation_id,
            org_id=prepared.org_id,
            role=MessageRole.USER,
            # The RAW question is stored: the user should see what they
            # typed. The rewrite is a retrieval implementation detail.
            content=prepared.raw_question,
        )
    )
    db.add(
        Message(
            id=uuid.uuid4(),
            conversation_id=prepared.conversation_id,
            org_id=prepared.org_id,
            role=MessageRole.ASSISTANT,
            content=answer,
            cited_chunk_ids=[uuid.UUID(str(c.chunk_id)) for c in citations] or None,
        )
    )
    db.commit()

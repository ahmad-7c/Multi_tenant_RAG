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
from app.services.assistance import Intent, classify, reply_for
from app.services.citations import Citation, validate_citations
from app.services.embedding import EmbeddingProvider, EmbeddingTask
from app.models.conversation import MessageKind
from app.services.llm import (
    LLMProvider,
    LLMSafetyBlockedError,
    safe_answer_for,
)
from app.services.prompting import INSUFFICIENT_CONTEXT_ANSWER, BuiltPrompt, build_prompt
from app.services.query_rewrite import rewrite_question
from app.services.reranker import (
    NOT_FOUND_ANSWER,
    Reranker,
    map_to_parents,
    rerank_with_fallback,
    scores_are_relevance,
    should_answer,
)
from app.services.document_service import list_ready_document_names
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
    # A greeting, "what can you do", or "thanks": answered from a
    # deterministic template with no retrieval and no model call.
    assisted: bool = False
    cache_hit: bool = False
    used_reranker_fallback: bool = False
    # None, not 0. On a cache hit nothing was retrieved and nothing was
    # mapped, and "candidates 0" is a false statement about the request
    # rather than an absent measurement -- the same reason the token
    # columns on usage_events are nullable. The UI hides a null and
    # would render a 0.
    candidate_count: int | None = None
    parent_count: int | None = None
    # The gate score: exactly the number compared to
    # CONFIDENCE_THRESHOLD. None unless it is a calibrated relevance.
    confidence: float | None = None
    # Whether THIS ranking's scores are relevance. Decided once, here,
    # because this is the only place that knows both which reranker ran
    # and whether its call fell back.
    scores_calibrated: bool = False
    # Per-stage wall clock, monotonic. None where the stage did not run.
    retrieve_ms: int | None = None
    rerank_ms: int | None = None


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
    # Conversational turns are intercepted FIRST, on the raw question,
    # before the rewriter, the cache, the embedder and retrieval. Three
    # reasons, in order of importance:
    #
    #   1. "hi" is not a question about a corpus, and answering it with
    #      "I could not find anything in your documents" reads as a
    #      broken product. That was a real transcript.
    #   2. It costs nothing -- no rewrite call, no embedding, no rerank,
    #      no generation.
    #   3. On the RAW text, because the rewriter would turn "hi" into
    #      something else entirely once there is history to rewrite
    #      against, and then the intent would no longer be visible.
    intent = classify(question)
    if intent is not Intent.NONE:

        def _document_names():
            db = session_factory()
            try:
                return list_ready_document_names(db, org_id=org_id)
            finally:
                db.close()

        names = await run_blocking(_document_names)
        return PreparedQuery(
            org_id=org_id,
            user_id=user_id,
            conversation_id=conversation_id,
            raw_question=question,
            # Not rewritten and not cached: the cache key is built from
            # a rewritten question, and these replies depend on the
            # org's CURRENT document list, which a cache would staple
            # to a stale snapshot.
            rewritten_question=question,
            was_rewritten=False,
            index_version=index_version,
            cache_key="",
            assisted=True,
            short_circuit_answer=reply_for(intent, document_names=names),
        )

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

    # QUERY, not the DOCUMENT default. Retrieval is asymmetric: a provider
    # that supports task types projects a short question and the long
    # passage that answers it into the same region only when it is told
    # which is which. Getting this wrong returns a valid vector in the
    # wrong part of the space -- nothing errors, answers are just quietly
    # worse.
    def _embed_query():
        return embedder.embed([rewritten], task=EmbeddingTask.QUERY)

    # Off the event loop, like the two DB calls below. embed() is
    # SYNCHRONOUS -- a blocking httpx POST, preceded by RateLimiter's
    # time.sleep() -- so calling it inline here stalled the whole loop
    # for the duration: every other in-flight request, every other
    # user's stream, stopped mid-token. Up to EMBED_TIMEOUT_SECONDS of it
    # in the worst case, and nothing in a test would have noticed,
    # because a fake embedder returns instantly.
    [query_vector] = await run_blocking(_embed_query)

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

    retrieve_started = time.monotonic()
    candidates = await run_blocking(_retrieve)
    prepared.retrieve_ms = int((time.monotonic() - retrieve_started) * 1000)
    prepared.candidate_count = len(candidates)

    rerank_started = time.monotonic()
    ranked, used_fallback = await rerank_with_fallback(reranker, rewritten, candidates)
    prepared.rerank_ms = int((time.monotonic() - rerank_started) * 1000)
    prepared.used_reranker_fallback = used_fallback

    # `bool(ranked)` is load-bearing, not defensive. The flag answers
    # "are the scores on this event real relevance values", and with an
    # empty ranking there are no scores to be calibrated OR
    # uncalibrated. `scores_are_relevance` can only see the reranker's
    # config and whether the call fell back; neither of those knows that
    # nothing came back. Without this an empty corpus reported
    # scores_calibrated=True alongside a null confidence, contradicting
    # both the field's documented meaning and the cache-hit path, which
    # reports False for exactly the same reason.
    prepared.scores_calibrated = bool(ranked) and scores_are_relevance(
        reranker, used_fallback
    )
    if prepared.scores_calibrated:
        # Published even when the gate then REFUSES: on a refusal this is
        # the most informative number available, because it says how
        # close the best match came to the threshold.
        prepared.confidence = ranked[0].score

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


@dataclass(frozen=True)
class AnswerOutcome:
    """What phase 2 produced.

    A dataclass rather than a tuple because this has now grown twice;
    positional unpacking at every call site makes each addition a
    silent-breakage risk.
    """

    answer: str
    citations: list[Citation]
    errored: bool
    # What to record on the assistant Message row. Drives whether the
    # turn is visible to the query rewriter later.
    kind: MessageKind
    # Time to the first streamed token, ms. None when no model call was
    # made (cache hit, gated refusal) and None when the stream FAILED:
    # the partial text is discarded, so a timing for it would describe
    # an answer nobody received.
    first_token_ms: int | None = None


async def generate_answer(
    prepared: PreparedQuery, *, llm: LLMProvider, usage_sink: dict | None = None
):
    """Phase 2. Yields ("token", str) then ("done", AnswerOutcome).

    No database session is open while this runs.

    `usage_sink` is passed straight through to the provider, which fills
    it with the provider's OWN token counts. It stays empty on a cache
    hit or a gated refusal, because no model call was made -- and an
    empty sink is what makes the usage row record NULL tokens rather
    than zero, which are different facts.
    """
    if prepared.short_circuit_answer is not None:
        # A cache hit or a gated refusal. Emitted as a single token event
        # so the client parses one format either way.
        yield "token", prepared.short_circuit_answer
        yield "done", AnswerOutcome(
            answer=prepared.short_circuit_answer,
            citations=prepared.short_circuit_citations,
            errored=False,
            # A gated "not found" is a REFUSAL, not an answer: the gate
            # declined on content grounds. Feeding "I could not find
            # anything..." to the rewriter makes the next question
            # retrieve against the phrasing of a non-answer.
            kind=(
                MessageKind.ASSIST
                if prepared.assisted
                else MessageKind.REFUSAL
                if prepared.gated
                else MessageKind.ANSWER
            ),
        )
        return

    assert prepared.prompt is not None
    pieces: list[str] = []
    failure: BaseException | None = None
    first_token_ms: int | None = None
    stream_started = time.monotonic()
    try:
        async for piece in llm.stream(
            system=prepared.prompt.system,
            user=prepared.prompt.user,
            max_tokens=settings.LLM_MAX_TOKENS,
            temperature=settings.LLM_TEMPERATURE,
            usage_sink=usage_sink,
        ):
            if first_token_ms is None:
                # Measured at the first piece the PROVIDER yielded, so
                # this is time-to-first-token of the model call and not
                # of the HTTP response -- the number that explains a
                # chat UI that feels slow.
                first_token_ms = int((time.monotonic() - stream_started) * 1000)
            pieces.append(piece)
            yield "token", piece
    except asyncio.CancelledError:
        # The client disconnected. Re-raised rather than swallowed so the
        # cancellation propagates into the provider's own coroutine and the
        # upstream request is actually aborted -- otherwise we keep paying
        # for tokens nobody will read.
        logger.info("answer stream cancelled; aborting the upstream LLM call")
        raise
    except Exception as exc:
        # Logged with the exception, answered without it: the user gets a
        # sentence, the operator gets the stack trace and the status code.
        logger.warning("LLM stream failed", exc_info=True)
        failure = exc

    if failure is not None:
        # Any partial text is DISCARDED, even though it was already
        # streamed. The model stopped because it would not continue, and
        # half a refused answer reads exactly like a whole one. The route
        # sends the authoritative answer in its final `citations` event
        # precisely so what was already printed can be superseded.
        #
        # No citations either: citing sources for text that was never
        # generated would be fabricated provenance.
        yield "done", AnswerOutcome(
            answer=safe_answer_for(failure),
            citations=[],
            errored=True,
            # A safety block is the model declining on CONTENT grounds,
            # which is a refusal; a rate limit or timeout is
            # infrastructure, which is an error. Both are hidden from
            # the rewriter -- the split is for the operator reading the
            # table, where "we refuse 20% of questions" and "the
            # provider drops 20% of calls" need very different fixes.
            kind=(
                MessageKind.REFUSAL
                if isinstance(failure, LLMSafetyBlockedError)
                else MessageKind.ERROR
            ),
        )
        return

    raw_answer = "".join(pieces)

    # Validation runs after streaming on pure text, which is why
    # citations.py needs no database and can run with the session closed.
    answer, citations = validate_citations(
        raw_answer,
        prepared.prompt.blocks,
        # The scores reach the client only if this ranking's scores are
        # relevance at all. Decided in phase 1 and carried, not
        # re-derived here where the reranker is out of scope.
        expose_scores=prepared.scores_calibrated,
    )
    if not answer.strip():
        answer = INSUFFICIENT_CONTEXT_ANSWER
    # errored=False: reaching here means the stream completed. Every
    # failure path returned above with its own specific message.
    yield "done", AnswerOutcome(
        answer=answer,
        citations=citations,
        errored=False,
        first_token_ms=first_token_ms,
        # An answer the citation validator emptied is not an answer: the
        # model cited nothing the server built, so INSUFFICIENT_CONTEXT
        # is what the user reads and a refusal is what it is.
        kind=MessageKind.ANSWER if citations else MessageKind.REFUSAL,
    )


def _citation_to_json(citation: Citation) -> dict:
    """A Citation as JSON-safe primitives.

    Every field that may hold a UUID is stringified by ITERATING the
    dataclass, not by listing ids by hand. The hand-written version
    named `document_id` and `chunk_id`; B1 added `matched_chunk_id`
    and every cache write then failed with "Object of type UUID is
    not JSON serializable". Because the write is deliberately
    non-fatal, the only symptom was a cache that never stored
    anything and answers that were always recomputed -- found in a
    log during end-to-end testing, not by a user.
    """
    payload = asdict(citation)
    for key, value in payload.items():
        if isinstance(value, uuid.UUID):
            payload[key] = str(value)
    return payload


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
            # Serialised to JSON, so every UUID has to become a string.
            citations=[_citation_to_json(c) for c in citations],
            model=model,
            cached_at=time.time(),
        ),
        settings.CACHE_TTL_SECONDS,
    )


def persist_turn(
    db,
    *,
    prepared: PreparedQuery,
    answer: str,
    citations: list[Citation],
    kind: MessageKind = MessageKind.ANSWER,
) -> None:
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
            # Refusals and errors are stored (the user saw them) but
            # carry a kind that keeps them out of the rewrite window.
            kind=kind,
            cited_chunk_ids=[uuid.UUID(str(c.chunk_id)) for c in citations] or None,
        )
    )
    db.commit()

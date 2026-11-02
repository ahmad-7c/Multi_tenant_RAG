"""Conversations and the streaming answer endpoint.

Why SSE and not WebSockets: the data flows one way (server -> client,
token after token) and the request that starts it is an ordinary POST.
SSE is plain HTTP, so it keeps bearer auth, CORS, proxies, HTTP/2
multiplexing and every piece of request-scoped middleware working
unchanged. A WebSocket upgrade throws all of that away and buys
bidirectionality this feature does not use -- the client sends exactly one
thing, the question, and it already sent it in the POST body. It also
reconnects by itself and survives a proxy hiccup, which a socket does not.

The Nginx gotcha: `proxy_buffering` is ON by default, so Nginx will hold
the response until its buffer fills or the upstream closes -- turning a
token stream into one delayed blob and making the whole feature look
broken while every byte is technically delivered. Hence
`X-Accel-Buffering: no` on the response below, which Nginx honours
per-response. Other proxies need their own equivalent
(`proxy_pass_request_buffering`, CloudFront buffering policies, etc.), so
the header is necessary but not by itself sufficient.
"""
import asyncio
import json
import time
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.limits import get_limiter, limit_answers, limit_default
from app.core.config import settings
from app.db.session import SessionLocal, get_db
from app.models.conversation import MessageKind
from app.models.organization import Organization
from app.models.user import User
from app.models.usage_event import UsageStatus
from app.services import usage_service
from app.services.rate_limit import RedisUnavailable
from app.schemas.conversation import (
    CitationsEvent,
    ConversationResponse,
    CreateConversationRequest,
    CreateMessageRequest,
    MessageResponse,
)
from app.services import conversation_service, query_service
from app.services.cache import get_answer_cache
from app.services.embedding import get_embedding_provider
from app.services.llm import get_llm, get_rewrite_llm
from app.services.reranker import get_reranker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])

# 404, not 403, for another org's or another user's conversation: a 403
# confirms the id exists and lets someone probe ids across tenants.
_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
)


@router.post(
    "",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_default)],
)
def create_conversation(
    payload: CreateConversationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return conversation_service.create_conversation(
        db, org_id=current_user.org_id, user_id=current_user.id, title=payload.title
    )


@router.get(
    "",
    response_model=list[ConversationResponse],
    dependencies=[Depends(limit_default)],
)
def list_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return conversation_service.list_conversations(
        db, org_id=current_user.org_id, user_id=current_user.id
    )


@router.get(
    "/{conversation_id}/messages",
    response_model=list[MessageResponse],
    dependencies=[Depends(limit_default)],
)
def list_messages(
    conversation_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = conversation_service.get_conversation(
        db,
        org_id=current_user.org_id,
        user_id=current_user.id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise _NOT_FOUND
    return conversation_service.list_messages(
        db, org_id=current_user.org_id, conversation_id=conversation_id
    )


def _sse(event: str, data: dict) -> str:
    # One JSON object per event, so a token containing a newline cannot
    # split the frame.
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _usage_status(outcome) -> UsageStatus:
    """Map what the user got onto what was billable.

    Deliberately not the same thing as `messages.kind`, which records
    what the user was SHOWN. A gate refusal is a refusal to the user and
    still cost an embedding and a rerank; a provider timeout is an error
    and cost a partial generation. Both are billable, differently.
    """
    if outcome.errored:
        return UsageStatus.ERROR
    if outcome.kind == MessageKind.REFUSAL:
        return UsageStatus.REFUSED
    # ASSIST falls through to OK deliberately. It succeeded, and it
    # cost nothing -- no embedding, no rerank, no generation -- so the
    # token columns stay NULL and the dashboard's refusal rate is not
    # inflated by people saying hello.
    return UsageStatus.OK


def _output_tokens(usage: dict) -> int | None:
    """Answer tokens, plus thinking tokens when the provider reports them.

    Thinking tokens are BILLED and are not part of the answer, so a cost
    figure that omits them is wrong in the expensive direction. Measured
    absent on gemini-3.5-flash-lite (which also rejects
    `thinkingBudget: 0` with a 400), so this is normally just
    candidatesTokenCount -- and is correct the day it is not.
    """
    candidates = usage.get("candidatesTokenCount")
    thoughts = usage.get("thoughtsTokenCount")
    if candidates is None and thoughts is None:
        return None
    return (candidates or 0) + (thoughts or 0)


def _citation_payload(citation, *, expose_score: bool) -> dict:
    return {
        "marker": citation.marker,
        "document": citation.document,
        "document_id": str(citation.document_id),
        "chunk_id": str(citation.chunk_id),
        "page": citation.page,
        "snippet": citation.snippet,
        "passage": citation.passage,
        "matched_chunk_id": (
            str(citation.matched_chunk_id)
            if citation.matched_chunk_id is not None
            else None
        ),
        # Nulled a SECOND time here, on purpose. validate_citations
        # already withheld it for a live uncalibrated ranking, but a
        # citation replayed from the cache carries a score from whatever
        # reranker configuration filled that key -- which cannot be
        # verified now. Both gates read the same flag, so neither is
        # load-bearing alone.
        "score": citation.score if expose_score else None,
    }


def build_citations_payload(prepared, outcome) -> dict:
    """The authoritative final event for one turn.

    A function rather than an inline dict because it is the whole
    contract between the backend and the UI's trace chips and source
    panel, and it needs testing directly -- driving the full SSE route
    to assert that one number is null is a slow way to pin a mapping.

    Every value here was MEASURED. Anything the request did not do is
    null: a cache hit returns early from phase 1, so its counts and
    timings were never assigned and stay None rather than reading 0.
    """
    # On a cache hit this is False because phase 1 returned before
    # setting it -- which is correct and not incidental: nothing was
    # scored for THIS request.
    expose_scores = prepared.scores_calibrated
    return {
        "answer": outcome.answer,
        "citations": [
            _citation_payload(c, expose_score=expose_scores)
            for c in outcome.citations
        ],
        "cached": prepared.cache_hit,
        "gated": prepared.gated,
        # What this turn IS, mirroring messages.kind. The UI needs it
        # to tell a grounded answer from a refusal from a greeting --
        # `gated` alone cannot, because an assist turn is neither
        # gated nor an answer.
        "kind": outcome.kind.value if outcome.kind else None,
        "rewritten_question": (
            prepared.rewritten_question if prepared.was_rewritten else None
        ),
        "confidence": prepared.confidence,
        "scores_calibrated": expose_scores,
        "used_reranker_fallback": prepared.used_reranker_fallback,
        "candidate_count": prepared.candidate_count,
        "parent_count": prepared.parent_count,
        "retrieve_ms": prepared.retrieve_ms,
        "rerank_ms": prepared.rerank_ms,
        "first_token_ms": outcome.first_token_ms,
    }


@router.post(
    "/{conversation_id}/messages",
    # `responses`, never `response_model`: a response_model would make
    # FastAPI validate and serialise a return value, which for a
    # StreamingResponse means buffering the whole answer and losing the
    # stream. This is documentation only -- it publishes the contract the
    # BFF codes against without touching what the route sends.
    # Without this FastAPI also advertises application/json for 200,
    # which this route never returns.
    response_class=StreamingResponse,
    responses={
        200: {
            "model": CitationsEvent,
            "description": (
                "A `text/event-stream`: zero or more `token` events, then "
                "exactly one authoritative `citations` event, then `done`. "
                "The schema shown is the `data` payload of the `citations` "
                "event. Render citation markers from it rather than from "
                "the streamed text, which may contain a marker validation "
                "dropped."
            ),
            "content": {"text/event-stream": {}},
        }
    },
)
async def create_message(
    conversation_id: uuid.UUID,
    payload: CreateMessageRequest,
    request: Request,
    # limit_answers REPLACES get_current_user rather than sitting beside
    # it. Two dependencies resolving the same bearer token would decode
    # it twice, and -- the real reason -- it would be possible to delete
    # the limit while leaving authentication in place, which looks
    # correct in a diff.
    current_user: User = Depends(limit_answers),
):
    # Set at startup by the embedding-model guard in main.py. Refusing is
    # the honest response: every stored vector was produced by a different
    # model than the one that will embed this question, so retrieval would
    # return noise and the LLM would write confident prose over it. A
    # degraded answer nobody can tell is degraded is worse than a 503.
    model_error = getattr(request.app.state, "embedding_model_error", None)
    if model_error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Search is unavailable: the stored document index was built "
                "with a different embedding model. An administrator must "
                "re-ingest the documents."
            ),
        )

    org_id = current_user.org_id
    user_id = current_user.id

    # ---- phase 1: everything that needs the database -------------------
    # A session opened and closed by hand rather than via Depends(get_db),
    # because FastAPI holds a dependency-provided session for the whole
    # response -- including the entire stream, which is exactly what must
    # not happen.
    def _load():
        db = SessionLocal()
        try:
            conversation = conversation_service.get_conversation(
                db, org_id=org_id, user_id=user_id, conversation_id=conversation_id
            )
            if conversation is None:
                return None, None, None
            history = conversation_service.history_for_rewrite(
                db,
                org_id=org_id,
                conversation_id=conversation_id,
                turns=settings.REWRITE_HISTORY_TURNS,
            )
            organization = db.get(Organization, org_id)
            return conversation.id, history, organization.index_version
        finally:
            db.close()

    # psycopg2 is a SYNCHRONOUS driver: every query blocks the thread it
    # runs on. Running it in the default threadpool keeps the event loop
    # free, which is the difference between one slow query delaying one
    # request and it delaying all of them.
    found_id, history, index_version = await asyncio.to_thread(_load)
    if found_id is None:
        raise _NOT_FOUND

    # prepare_query is async (it awaits the rewriter, the cache and the
    # reranker) but its DB steps are blocking. It is handed a session
    # FACTORY rather than a session, and pushes each blocking step onto a
    # worker thread that opens and closes its own session there -- so no
    # Session ever crosses a thread boundary, and the loop stays free.
    prepared = await query_service.prepare_query(
        session_factory=SessionLocal,
        org_id=org_id,
        user_id=user_id,
        conversation_id=conversation_id,
        question=payload.content,
        history=history,
        index_version=index_version,
        embedder=get_embedding_provider(),
        reranker=get_reranker(),
        rewrite_llm=get_rewrite_llm(),
        cache=get_answer_cache(),
    )
    # THE boundary. Past this line there is no DB session until phase 3.

    llm = get_llm()
    cache = get_answer_cache()

    # ---- the concurrency slot --------------------------------------
    # Acquired here and not in a dependency, because it must be RELEASED
    # when the stream ends, and a FastAPI dependency's teardown runs
    # before the StreamingResponse body does. A slot released at that
    # point is a slot that was never held.
    #
    # A cache hit still takes a slot: it is cheap, but it is one more
    # concurrent response, and a cap that exempts the cheap path is a
    # cap one tenant can walk straight through.
    # Measured from here: after auth, the limiter and the model guard,
    # because what is being metered is the WORK, and a request rejected
    # by the limiter did none. Those are recorded separately, with
    # status REJECTED and no latency.
    started_at = time.monotonic()
    # One dict per request, owned by this handler. The provider is a
    # process-wide singleton, so usage recorded on the provider itself
    # would mix two tenants' tokens under concurrency.
    usage: dict = {}

    slot: str | None = None
    slot_degraded = False
    if settings.RATE_LIMIT_ENABLED:
        limiter = get_limiter()
        try:
            slot = await limiter.acquire_stream_slot(
                str(org_id), limit=settings.MAX_CONCURRENT_STREAMS_PER_ORG
            )
        except RedisUnavailable:
            # Fail OPEN on the slot specifically, unlike the rate limit.
            # The per-minute bucket has already failed closed onto a
            # per-process bucket, so spend is bounded; refusing here as
            # well would take the product down during a Redis incident
            # for a second-order protection.
            slot_degraded = True
            logger.warning(
                "stream slot could not be acquired (Redis unavailable); "
                "serving without a concurrency cap"
            )
        if slot is None and not slot_degraded:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "Too many answers are being generated for your "
                    "organisation at once. Please wait for one to finish."
                ),
                # No Retry-After: unlike a token bucket, this one has no
                # refill rate to compute a wait from -- it clears when
                # somebody else's answer finishes, which is unknowable
                # here. A guessed number would be worse than none.
            )

    async def _release_slot():
        if slot is not None:
            await get_limiter().release_stream_slot(str(org_id), slot)

    async def event_stream():
        outcome = query_service.AnswerOutcome(
            answer="", citations=[], errored=False, kind=MessageKind.ANSWER
        )
        try:
            # A cache hit is emitted as a single token event, deliberately:
            # the client parses one format whether the answer took three
            # seconds to generate or came back instantly.
            async for kind, value in query_service.generate_answer(
                prepared, llm=llm, usage_sink=usage
            ):
                if kind == "token":
                    yield _sse("token", {"text": value})
                else:
                    outcome = value
        except asyncio.CancelledError:
            # The client went away. Nothing is persisted and nothing is
            # cached: a half-streamed answer is not an answer, and the
            # upstream provider call is cancelled with this coroutine so we
            # stop paying for tokens nobody will read.
            logger.info("client disconnected mid-stream; abandoning the turn")
            raise

        # One final citations event, after the tokens. Authoritative: the
        # streamed text may contain a marker that validation dropped, so
        # the client renders markers from this payload rather than from
        # what it already printed.
        yield _sse("citations", build_citations_payload(prepared, outcome))
        yield _sse("done", {})

        await query_service.maybe_cache_answer(
            prepared,
            answer=outcome.answer,
            citations=outcome.citations,
            cache=cache,
            model=llm.model,
            errored=outcome.errored,
        )

        # ---- phase 3: a new, short session ---------------------------
        def _persist():
            session = SessionLocal()
            try:
                query_service.persist_turn(
                    session,
                    prepared=prepared,
                    answer=outcome.answer,
                    citations=outcome.citations,
                    kind=outcome.kind,
                )
            finally:
                session.close()

        await asyncio.to_thread(_persist)

        # ---- metering ------------------------------------------------
        # After the stream, on a worker thread, in its own short
        # session: the user is already served, so this costs them
        # nothing, and it never holds a session across the stream.
        #
        # Token counts come from `usage`, which the PROVIDER filled. An
        # empty dict means no model call happened (a cache hit, or the
        # gate refused) and the columns stay NULL -- "unknown" and
        # "zero" are different facts and the table keeps them apart.
        def _meter():
            session = SessionLocal()
            try:
                usage_service.record(
                    session,
                    org_id=org_id,
                    user_id=user_id,
                    endpoint=usage_service.ENDPOINT_ANSWER,
                    status=_usage_status(outcome),
                    embedding_tokens=None,
                    llm_input_tokens=usage.get("promptTokenCount"),
                    llm_output_tokens=_output_tokens(usage),
                    # One rerank call per answered question, zero when
                    # the answer came from cache -- the reranker runs
                    # inside prepare_query, which short-circuits on a
                    # hit.
                    rerank_calls=0 if prepared.cache_hit else 1,
                    cache_hit=prepared.cache_hit,
                    latency_ms=int((time.monotonic() - started_at) * 1000),
                )
            finally:
                session.close()

        await asyncio.to_thread(_meter)

        # ---- the budget counter --------------------------------------
        # Counted AFTER the work, checked BEFORE it (in limit_answers).
        # A cache hit is deliberately NOT counted: the guard exists to
        # protect a PROVIDER quota, and a cache hit touched no provider.
        # Counting it would make the cache -- the thing that saves
        # quota -- consume quota.
        if settings.RATE_LIMIT_ENABLED and not prepared.cache_hit:
            consume = getattr(get_limiter(), "consume_budget", None)
            if consume is not None:
                try:
                    await consume(str(org_id), units=1)
                except Exception:
                    # Losing a count under-reports spend for the day,
                    # which is a metering problem. Raising here would
                    # break a request the user has already received in
                    # full.
                    logger.warning("could not count budget usage", exc_info=True)

    async def guarded_stream():
        """event_stream(), with the slot released on EVERY exit path.

        Three of them, and all three happened in testing:
          * normal completion,
          * an exception anywhere in generation or persistence,
          * CancelledError, which is what a client closing the tab
            raises -- and the one a naive `await event_stream(); release`
            misses, because the cancellation propagates out of the
            await and the release line never runs.

        A leaked slot is not fatal (STREAM_SLOT_LUA expires members
        older than STREAM_SLOT_TTL_SECONDS) but it is a tenant losing
        one of three slots for three minutes, repeatedly, for as long as
        users close tabs.
        """
        try:
            async for chunk in event_stream():
                yield chunk
        finally:
            await _release_slot()

    return StreamingResponse(
        guarded_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Nginx buffers proxied responses by default, which would turn
            # this stream into a single delayed blob. See the module
            # docstring.
            "X-Accel-Buffering": "no",
        },
    )

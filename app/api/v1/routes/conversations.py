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
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.db.session import SessionLocal, get_db
from app.models.conversation import MessageKind
from app.models.organization import Organization
from app.models.user import User
from app.schemas.conversation import (
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


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
def create_conversation(
    payload: CreateConversationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return conversation_service.create_conversation(
        db, org_id=current_user.org_id, user_id=current_user.id, title=payload.title
    )


@router.get("", response_model=list[ConversationResponse])
def list_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return conversation_service.list_conversations(
        db, org_id=current_user.org_id, user_id=current_user.id
    )


@router.get("/{conversation_id}/messages", response_model=list[MessageResponse])
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


def _citation_payload(citation) -> dict:
    return {
        "marker": citation.marker,
        "document": citation.document,
        "document_id": str(citation.document_id),
        "chunk_id": str(citation.chunk_id),
        "page": citation.page,
        "snippet": citation.snippet,
    }


@router.post("/{conversation_id}/messages")
async def create_message(
    conversation_id: uuid.UUID,
    payload: CreateMessageRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
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

    async def event_stream():
        outcome = query_service.AnswerOutcome(
            answer="", citations=[], errored=False, kind=MessageKind.ANSWER
        )
        try:
            # A cache hit is emitted as a single token event, deliberately:
            # the client parses one format whether the answer took three
            # seconds to generate or came back instantly.
            async for kind, value in query_service.generate_answer(prepared, llm=llm):
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
        yield _sse(
            "citations",
            {
                "answer": outcome.answer,
                "citations": [_citation_payload(c) for c in outcome.citations],
                "cached": prepared.cache_hit,
                "gated": prepared.gated,
                "rewritten_question": (
                    prepared.rewritten_question if prepared.was_rewritten else None
                ),
            },
        )
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

    return StreamingResponse(
        event_stream(),
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

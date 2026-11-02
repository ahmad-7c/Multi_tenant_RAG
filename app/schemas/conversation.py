import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.conversation import MessageKind, MessageRole


class CreateConversationRequest(BaseModel):
    # No org_id and no user_id: both come from the verified token. Letting
    # a caller name either would be a cross-tenant escalation.
    title: Optional[str] = Field(default=None, max_length=500)


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: Optional[str]
    created_at: datetime


class CreateMessageRequest(BaseModel):
    # Bounded: the question is embedded and sent to a model, and an
    # unbounded string is both a cost and a token-limit problem.
    content: str = Field(min_length=1, max_length=4000)


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: MessageRole
    content: str
    # Exposed so a client can tell a refusal or an infrastructure error
    # from an answer. Without it the three are one grey bubble of prose
    # and the UI has to guess from the wording -- which is exactly the
    # heuristic `kind` exists to replace. NULL for user turns and for
    # rows written before migration 0006, so it is Optional on the wire
    # too rather than defaulted to ANSWER: claiming a pre-migration
    # refusal was an answer is worse than admitting we do not know.
    kind: Optional[MessageKind]
    cited_chunk_ids: Optional[list[uuid.UUID]]
    created_at: datetime


# ---------------------------------------------------------------------------
# The `citations` SSE event.
#
# DOCUMENTATION ONLY. These models are not used to serialise the event --
# the route builds a plain dict in `build_citations_payload`, because a
# StreamingResponse cannot carry a `response_model` and adding one would
# turn the stream into a buffered JSON body. They exist so the event has
# a published contract in OpenAPI for the BFF to code against, and
# `test_trace_payload.py` asserts the field names here match what the
# route actually emits -- otherwise this drifts into fiction.
# ---------------------------------------------------------------------------


class CitationPayload(BaseModel):
    marker: int = Field(
        description="The bracketed number as it appears in the answer text."
    )
    document: str = Field(description="Filename of the source document.")
    document_id: uuid.UUID
    chunk_id: uuid.UUID = Field(
        description="The PARENT chunk the model was shown."
    )
    page: int
    snippet: str = Field(
        description="Up to 300 characters of the parent, for a compact list."
    )
    passage: Optional[str] = Field(
        default=None,
        description=(
            "The parent passage the model actually read, for verification. "
            "Capped at 1200 characters including any ellipses and cut at "
            "sentence boundaries. When the parent exceeds the cap the "
            "window follows the matched child rather than the parent's "
            "start, and begins with '...' to show text precedes it."
        ),
    )
    matched_chunk_id: Optional[uuid.UUID] = Field(
        default=None,
        description=(
            "The CHILD chunk whose match caused this parent to be "
            "retrieved. Use it to highlight the matched sentence inside "
            "`passage`."
        ),
    )
    score: Optional[float] = Field(
        default=None,
        description=(
            "Cross-encoder relevance in roughly 0..1. Null whenever "
            "`scores_calibrated` is false -- under a passthrough reranker "
            "or a reranker fallback the underlying number is a rank "
            "reciprocal, not a relevance, and must not be rendered as one. "
            "Also null on a cache hit."
        ),
    )


class CitationsEvent(BaseModel):
    """The authoritative final event of an answer turn.

    Sent after the `token` events and before `done`. The streamed text
    may contain a marker that validation dropped, so a client renders
    markers from THIS payload rather than from what it already printed.
    """

    answer: str = Field(description="The validated answer. Supersedes streamed text.")
    citations: list[CitationPayload]
    cached: bool = Field(description="True if this answer was replayed from cache.")
    gated: bool = Field(
        description="True if the confidence gate refused before any model call."
    )
    kind: Optional[MessageKind] = Field(
        default=None,
        description=(
            "What this turn is, mirroring messages.kind. `answer` is "
            "grounded and cited; `refusal` declined on content grounds; "
            "`error` is infrastructure; `assist` is a conversational turn "
            "(a greeting, 'what can you do') answered without retrieval or "
            "a model call, so it carries no citations and no timings. "
            "`gated` alone cannot distinguish these -- an assist turn is "
            "neither gated nor an answer."
        ),
    )
    rewritten_question: Optional[str] = Field(
        default=None,
        description=(
            "The standalone question used for retrieval, when it differs "
            "from what the user typed. Null on a conversation's first turn, "
            "which is never rewritten."
        ),
    )
    confidence: Optional[float] = Field(
        default=None,
        description=(
            "The gate score: the top reranked result's relevance, the exact "
            "number compared against CONFIDENCE_THRESHOLD. Null unless "
            "`scores_calibrated` is true. Present on a gated refusal, where "
            "it says how close the best match came."
        ),
    )
    scores_calibrated: bool = Field(
        description=(
            "Whether `confidence` and the per-citation `score` are real "
            "relevance values. False under a passthrough reranker, under a "
            "reranker fallback, and on a cache hit. When false, hide every "
            "score and confidence element rather than rendering a zero."
        )
    )
    used_reranker_fallback: bool = Field(
        description="True if the reranker call failed and RRF order was used."
    )
    candidate_count: Optional[int] = Field(
        default=None,
        description="Child chunks retrieved. Null on a cache hit: none were.",
    )
    parent_count: Optional[int] = Field(
        default=None,
        description=(
            "Parent chunks sent to the model. Null when the step did not "
            "run (cache hit, or the gate refused first); 0 when it ran and "
            "returned nothing."
        ),
    )
    retrieve_ms: Optional[int] = Field(
        default=None, description="Hybrid search wall clock. Null on a cache hit."
    )
    rerank_ms: Optional[int] = Field(
        default=None, description="Rerank wall clock. Null on a cache hit."
    )
    first_token_ms: Optional[int] = Field(
        default=None,
        description=(
            "MODEL time-to-first-token, in milliseconds: measured from "
            "opening the provider stream to the first text chunk it "
            "yields. This is NOT user-perceived time to first text -- it "
            "excludes auth, rate limiting, retrieval and reranking (all "
            "reported separately on this event) and all network time to "
            "the browser. Measure perceived latency client-side and label "
            "this value as model first-token time. Null when no model call "
            "was made (cache hit, gated refusal), and null when the stream "
            "failed, because the partial text is discarded and a timing "
            "for it would describe an answer nobody received."
        ),
    )

"""Reranking and the confidence gate.

The retrieval funnel:

    ~thousands of child chunks in the org
        |  vector leg (HNSW, cosine)      -> RETRIEVE_PER_LEG
        |  keyword leg (GIN, ts_rank)     -> RETRIEVE_PER_LEG
        v
    RRF merge                             -> RETRIEVE_CANDIDATES (~50)
        |  cross-encoder rerank
        v
    RERANK_TOP_N children (~8)
        |  map to parents, dedupe
        v
    a handful of parents -> the prompt

Each stage is cheaper per item and worse at judging than the next. The
first two are bi-encoder/lexical: they score the question and the chunk
*independently*, which is what makes them indexable but also means they
never actually compare the two. The reranker is a cross-encoder: it reads
question and chunk together and can tell that a chunk mentioning
"reimbursement" is not an answer to "who approves reimbursement". That is
far too expensive to run over the whole corpus and exactly right over 50
candidates.
"""
import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from functools import lru_cache

from app.core.config import settings
from app.services.retrieval import Candidate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RankedCandidate:
    candidate: Candidate
    # Semantics are reranker-specific: a hosted cross-encoder returns a
    # calibrated 0..1 relevance, while passthrough returns the RRF score,
    # which is ~0.016 at best. The gate therefore has to know which
    # reranker produced the score (see should_answer).
    score: float
    rank: int


class Reranker(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def scores_are_calibrated(self) -> bool:
        """True if `score` is a comparable relevance in roughly 0..1.

        Passthrough returns RRF scores, which are NOT relevance -- they are
        rank reciprocals whose magnitude depends only on how many lists a
        chunk appeared in. Comparing those against CONFIDENCE_THRESHOLD
        would gate every answer on an arithmetic accident.
        """

    @abstractmethod
    async def rerank(
        self, query: str, candidates: list[Candidate], top_n: int
    ) -> list[RankedCandidate]: ...


class PassthroughReranker(Reranker):
    """Keeps the RRF order. The honest no-op.

    Also the fallback when a hosted reranker times out: RRF order is
    already a reasonable hybrid ranking, so degrading to it answers the
    question slightly worse instead of not at all.
    """

    @property
    def name(self) -> str:
        return "passthrough"

    @property
    def scores_are_calibrated(self) -> bool:
        return False

    async def rerank(
        self, query: str, candidates: list[Candidate], top_n: int
    ) -> list[RankedCandidate]:
        return [
            RankedCandidate(candidate=c, score=c.rrf_score, rank=i)
            for i, c in enumerate(candidates[:top_n], start=1)
        ]


class FakeReranker(Reranker):
    """Deterministic reranker for tests.

    Scores by word overlap between query and chunk. Crude, but it is a
    genuine function of BOTH inputs, which is the property that matters:
    a fake that just echoed the input order could not distinguish "the
    reranker reordered things" from "the reranker did nothing".
    """

    def __init__(self, *, scores_calibrated: bool = True, fail: bool = False, delay: float = 0.0):
        self._calibrated = scores_calibrated
        self._fail = fail
        self._delay = delay
        self.calls: list[tuple[str, int]] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def scores_are_calibrated(self) -> bool:
        return self._calibrated

    async def rerank(
        self, query: str, candidates: list[Candidate], top_n: int
    ) -> list[RankedCandidate]:
        self.calls.append((query, len(candidates)))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError("fake reranker failure")

        query_words = {w.lower().strip(".,:;!?|") for w in query.split() if w}
        scored = []
        for candidate in candidates:
            words = {w.lower().strip(".,:;!?|") for w in candidate.content.split() if w}
            overlap = len(query_words & words)
            denominator = len(query_words) or 1
            scored.append((min(overlap / denominator, 1.0), candidate))
        # Sorted by score, with the original order as a stable tie-break.
        scored.sort(key=lambda item: -item[0])
        return [
            RankedCandidate(candidate=c, score=s, rank=i)
            for i, (s, c) in enumerate(scored[:top_n], start=1)
        ]


class HostedReranker(Reranker):
    """Cohere-style hosted rerank API.

    Request/response shape follows Cohere v2 /rerank, which several
    providers (Jina, Voyage, Together) accept or closely mirror, so the
    endpoint is configurable rather than hardcoded.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float,
        client=None,
    ):
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._timeout = timeout
        self._client = client

    @property
    def name(self) -> str:
        return f"hosted:{self._model}"

    @property
    def scores_are_calibrated(self) -> bool:
        return True

    async def rerank(
        self, query: str, candidates: list[Candidate], top_n: int
    ) -> list[RankedCandidate]:
        # Imported lazily so a deployment using passthrough does not need
        # httpx present at import time.
        import httpx

        payload = {
            "model": self._model,
            "query": query,
            "documents": [c.content for c in candidates],
            "top_n": min(top_n, len(candidates)),
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}

        # An async HTTP call, not a blocking one. This runs inside the
        # request handler, and a blocking 3-second POST would stall the
        # whole event loop -- every other in-flight request included.
        if self._client is not None:
            response = await self._client.post(
                self._base_url, json=payload, headers=headers
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    self._base_url, json=payload, headers=headers
                )
        response.raise_for_status()
        body = response.json()

        ranked = []
        for position, item in enumerate(body["results"], start=1):
            index = item["index"]
            if not 0 <= index < len(candidates):
                # A provider returning an out-of-range index would
                # otherwise attach a score to the wrong chunk.
                raise ValueError(f"reranker returned index {index} out of range")
            ranked.append(
                RankedCandidate(
                    candidate=candidates[index],
                    score=float(item["relevance_score"]),
                    rank=position,
                )
            )
        return ranked


async def rerank_with_fallback(
    reranker: Reranker,
    query: str,
    candidates: list[Candidate],
    *,
    top_n: int | None = None,
    timeout: float | None = None,
) -> tuple[list[RankedCandidate], bool]:
    """Rerank, falling back to RRF order on timeout or error.

    Returns (ranked, used_fallback).

    The reranker is a quality improvement, not a correctness requirement.
    A provider outage must not take the product down, so a timeout
    degrades to the RRF ordering we already have. The caller is told, via
    the flag, because a fallback also means the scores are no longer
    calibrated and the confidence gate has to behave differently.
    """
    top_n = settings.RERANK_TOP_N if top_n is None else top_n
    timeout = settings.RERANKER_TIMEOUT_SECONDS if timeout is None else timeout

    if not candidates:
        return [], False

    try:
        # wait_for, not a client-level timeout alone: it bounds the whole
        # operation including connection setup, DNS and retries inside the
        # SDK, which a per-read timeout does not.
        ranked = await asyncio.wait_for(
            reranker.rerank(query, candidates, top_n), timeout=timeout
        )
        return ranked, False
    except asyncio.TimeoutError:
        logger.warning(
            "reranker %s timed out after %.1fs; falling back to RRF order",
            reranker.name,
            timeout,
        )
    except Exception:
        logger.warning(
            "reranker %s failed; falling back to RRF order", reranker.name, exc_info=True
        )

    fallback = await PassthroughReranker().rerank(query, candidates, top_n)
    return fallback, True


@dataclass(frozen=True)
class RetrievedParent:
    """A parent chunk promoted from its best-ranked child."""

    parent_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    page_number: int
    content: str
    token_count: int
    best_rank: int
    best_score: float
    # Which child caused this parent to be selected. Kept so a citation
    # can quote the specific sentence that matched rather than the whole
    # parent.
    matched_chunk_id: uuid.UUID


def map_to_parents(
    db, ranked: list[RankedCandidate], *, org_id: uuid.UUID
) -> list[RetrievedParent]:
    """Children -> their parents, deduplicated, best rank wins.

    Deduplication is not an optimisation. Several children of one parent
    routinely survive reranking -- they overlap by design (CHILD_OVERLAP),
    so a strong passage produces several strong children. Without
    dedupe, that one passage would occupy three or four context slots and
    crowd out every other document, and the LLM would see the same text
    repeated, which reads as corroboration it has not got.
    """
    from sqlalchemy import text as sql_text

    best: dict[uuid.UUID, RankedCandidate] = {}
    for item in ranked:
        parent_id = item.candidate.parent_id
        # `ranked` is in rank order, so the first time a parent is seen is
        # its best rank.
        if parent_id not in best:
            best[parent_id] = item

    if not best:
        return []

    rows = db.execute(
        sql_text(
            """
            SELECT c.id, c.document_id, d.filename, c.page_number,
                   c.content, c.token_count
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id AND d.org_id = c.org_id
            WHERE c.org_id = :org_id AND c.id = ANY(:ids)
            """
        ),
        # org_id re-applied even though these ids came from an org-scoped
        # search: the tenant filter belongs on every query touching tenant
        # data, not only the first one in a chain.
        {"org_id": str(org_id), "ids": list(best.keys())},
    ).all()
    by_id = {row[0]: row for row in rows}

    parents = []
    for parent_id, item in sorted(best.items(), key=lambda kv: kv[1].rank):
        row = by_id.get(parent_id)
        if row is None:
            # Deleted between the search and now. Dropping it beats citing
            # a document that no longer exists.
            continue
        parents.append(
            RetrievedParent(
                parent_id=row[0],
                document_id=row[1],
                filename=row[2],
                page_number=row[3],
                content=row[4],
                token_count=row[5],
                best_rank=item.rank,
                best_score=item.score,
                matched_chunk_id=item.candidate.chunk_id,
            )
        )
    return parents


NOT_FOUND_ANSWER = (
    "I could not find anything in your organization's documents that answers "
    "this question."
)


def should_answer(
    ranked: list[RankedCandidate],
    *,
    reranker: Reranker,
    used_fallback: bool,
    threshold: float | None = None,
) -> bool:
    """The confidence gate.

    Retrieval ALWAYS returns its top-k. "Nothing in the corpus is
    relevant" and "this is exactly the answer" produce identically shaped
    results -- 50 candidates either way. So without a gate the LLM is
    handed the least-bad noise in the corpus and, being a language model,
    writes fluent confident prose over it. The gate is what turns "no
    relevant content" into an honest answer instead of a hallucination.

    It also saves the money: a gated question costs one embedding call and
    two index scans, not a full generation over several thousand tokens of
    context.

    CONFIDENCE_THRESHOLD is RERANKER-SPECIFIC and currently a guess. A
    Cohere rerank-v3.5 score of 0.3 does not mean what a Jina or Voyage
    0.3 means, and neither is comparable to a local cross-encoder's logit.
    Calibrating it needs a labelled set of question/should-answer pairs,
    which is Phase 4 (eval) work. Until then the default is deliberately
    permissive: a gate that is too tight silently refuses answerable
    questions, which is harder to notice than an occasional weak answer.
    """
    if not ranked:
        return False

    # Uncalibrated scores cannot be compared to the threshold at all. RRF
    # scores top out around 1/(k+1) ~= 0.016, so testing them against 0.3
    # would gate EVERY question -- the system would answer nothing. When
    # the score is not a relevance, having retrieved anything at all is
    # the only signal available, so the gate opens.
    if used_fallback or not reranker.scores_are_calibrated:
        return True

    threshold = settings.CONFIDENCE_THRESHOLD if threshold is None else threshold
    return ranked[0].score >= threshold


@lru_cache(maxsize=1)
def get_reranker() -> Reranker:
    provider = settings.RERANKER_PROVIDER
    if provider == "fake":
        return FakeReranker()
    if provider == "hosted":
        if not settings.RERANKER_API_KEY:
            # Fail loudly at startup rather than degrading to passthrough
            # on every request without anyone noticing the quality drop.
            raise RuntimeError("RERANKER_PROVIDER=hosted requires RERANKER_API_KEY")
        return HostedReranker(
            api_key=settings.RERANKER_API_KEY,
            model=settings.RERANKER_MODEL,
            base_url=settings.RERANKER_BASE_URL,
            timeout=settings.RERANKER_TIMEOUT_SECONDS,
        )
    return PassthroughReranker()

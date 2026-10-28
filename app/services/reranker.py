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


class RerankerError(Exception):
    """Base class for hosted-reranker failures.

    The split below is not cosmetic. Every one of these ends in the same
    place -- fall back to RRF order and answer anyway -- so the exception
    type cannot change what the user gets. What it changes is what the
    OPERATOR gets: a 429 is a blip that resolves itself and deserves a
    warning, while a 401 means reranking is off until someone edits an
    env var and deserves an error plus a stop to calling.
    """


class RerankerTransientError(RerankerError):
    """Might well succeed on the next request. Retry by simply existing."""


class RerankerRateLimitError(RerankerTransientError):
    """429. On a Cohere trial key this is 10 req/min, or the monthly cap.

    Deliberately transient even though an exhausted monthly quota is not:
    the status code alone cannot tell the two apart, and treating a
    minute-long throttle as permanent would disable reranking for the
    life of the process over a momentary spike.
    """


class RerankerUpstreamError(RerankerTransientError):
    """5xx, or any status we have no specific reading for."""


class RerankerTimeoutError(RerankerTransientError):
    """The provider did not answer inside the budget."""


class RerankerPermanentError(RerankerError):
    """Cannot succeed until configuration changes.

    A bad key, a retired model name, a wrong URL. Retrying is pure
    latency: the same request will fail the same way every time.
    """


class RerankerAuthError(RerankerPermanentError):
    """401/403. Key missing, revoked, or not entitled to this model."""


class RerankerRequestError(RerankerPermanentError):
    """400/404/422. Our payload or our URL is wrong, not their service."""


class RerankerDisabledError(RerankerPermanentError):
    """Raised without an HTTP call because the breaker is open.

    Its own class so the fallback path can log it at DEBUG. The ERROR
    was already emitted once, when the breaker tripped; repeating it on
    every query would bury the cause under its own consequences.
    """


class RerankerProtocolError(RerankerError):
    """The response parsed as JSON but was not the shape we expect.

    Neither transient nor permanent on purpose, so it does NOT trip the
    breaker. A captive-portal login page or a proxy error page during an
    outage arrives looking exactly like this, and that resolves on its
    own -- but so does a wrong base URL look like this forever. Falling
    back every time and logging loudly is the honest response to not
    knowing which.
    """


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


def classify_rerank_status(status: int) -> RerankerError:
    """HTTP status -> the exception that says what to do about it.

    Unknown statuses land on UPSTREAM, not on permanent. Guessing
    "permanent" on a status we have never seen would let one unfamiliar
    response from a proxy disable reranking for the life of the process.
    """
    if status in (401, 403):
        return RerankerAuthError(
            f"reranker rejected our credentials ({status}); "
            "check RERANKER_API_KEY"
        )
    if status in (400, 404, 422):
        # 404 covers the wrong-model-name case as well as the wrong-URL
        # case: Cohere routes by model inside the body, but a retired
        # model and a typo'd path both surface here.
        return RerankerRequestError(
            f"reranker refused our request ({status}); "
            "check RERANKER_MODEL and RERANKER_BASE_URL"
        )
    if status == 429:
        return RerankerRateLimitError("reranker rate limited (429)")
    if status == 408:
        return RerankerTimeoutError("reranker reported a request timeout (408)")
    return RerankerUpstreamError(f"reranker returned {status}")


class HostedReranker(Reranker):
    """Cohere v2 /rerank.

    Request/response shape follows Cohere v2 /rerank, which several
    providers (Jina, Voyage, Together) accept or closely mirror, so the
    endpoint is configurable rather than hardcoded.

    Checked against the published v2 contract rather than remembered:
    `documents` is a list of STRINGS (v1's document objects are gone),
    per-document truncation is `max_tokens_per_doc` (v1 called it
    `max_chunks_per_doc`), at most 1000 documents are accepted, and the
    response is `results[].index` / `results[].relevance_score`.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float,
        client=None,
        max_tokens_per_doc: int | None = None,
        max_documents: int | None = None,
    ):
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._timeout = timeout
        self._client = client
        self._max_tokens_per_doc = (
            settings.RERANKER_MAX_TOKENS_PER_DOC
            if max_tokens_per_doc is None
            else max_tokens_per_doc
        )
        self._max_documents = (
            settings.RERANKER_MAX_DOCUMENTS if max_documents is None else max_documents
        )
        # The breaker. Holds the reason string once a permanent failure is
        # seen; cleared only by a process restart, which is what fixing
        # the cause requires anyway since env vars are read at startup.
        # Instance state rather than module state because get_reranker()
        # is lru_cached to one instance per process, so here the two are
        # the same thing -- and a test gets a clean breaker by building a
        # new reranker.
        self._disabled_reason: str | None = None

    @property
    def name(self) -> str:
        return f"hosted:{self._model}"

    @property
    def scores_are_calibrated(self) -> bool:
        return True

    @property
    def is_disabled(self) -> bool:
        return self._disabled_reason is not None

    def _trip_breaker(self, exc: RerankerPermanentError) -> None:
        if self._disabled_reason is not None:
            return
        # Deliberately silent. The ERROR belongs to rerank_with_fallback,
        # which sees this failure exactly once -- the next call raises
        # RerankerDisabledError instead. Logging in both places produced
        # two ERRORs for one event, which is how a log loses its meaning.
        self._disabled_reason = str(exc)

    async def rerank(
        self, query: str, candidates: list[Candidate], top_n: int
    ) -> list[RankedCandidate]:
        # Imported lazily so a deployment using passthrough does not need
        # httpx present at import time.
        import httpx

        if self._disabled_reason is not None:
            raise RerankerDisabledError(self._disabled_reason)

        # Blank documents are a 400 from Cohere, and one of them would
        # cost us reranking for the WHOLE query rather than for itself.
        # The filtered list is kept so a returned index still resolves to
        # the candidate we actually sent: indices refer to the list as
        # submitted, so filtering without remapping would attach scores
        # to the wrong chunks -- silently, and in the right-looking order.
        sent = [c for c in candidates if c.content and c.content.strip()]
        if not sent:
            return []
        if len(sent) > self._max_documents:
            logger.warning(
                "reranker: %d candidates exceeds the %d-document limit; "
                "truncating. RETRIEVE_CANDIDATES is probably misconfigured.",
                len(sent),
                self._max_documents,
            )
            sent = sent[: self._max_documents]

        payload = {
            "model": self._model,
            "query": query,
            "documents": [c.content for c in sent],
            "top_n": min(top_n, len(sent)),
            "max_tokens_per_doc": self._max_tokens_per_doc,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            # Both sent explicitly: httpx sets Content-Type for json=, but
            # the v2 contract names both, and a provider that content-
            # negotiates to something other than JSON would then fail in
            # the parser instead of at the request.
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # An async HTTP call, not a blocking one. This runs inside the
        # request handler, and a blocking 3-second POST would stall the
        # whole event loop -- every other in-flight request included.
        try:
            if self._client is not None:
                response = await self._client.post(
                    self._base_url, json=payload, headers=headers
                )
            else:
                # The shared pooled client. Not `async with`: that would
                # close the pool the LLM and the embedder are using. The
                # 3-second budget rides the request instead of the
                # client, which is what lets one pool serve providers
                # whose timeouts differ by 10x.
                from app.core.http import get_async_client

                response = await get_async_client().post(
                    self._base_url,
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                )
        except httpx.TimeoutException as exc:
            # httpx timeouts are NOT TimeoutError subclasses, so without
            # this they reach the caller's generic handler and get logged
            # as an unexplained failure instead of as a timeout.
            raise RerankerTimeoutError(f"reranker request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            # Connect failures, DNS, protocol violations. Transient: the
            # next request may route somewhere healthier.
            raise RerankerUpstreamError(f"reranker request failed: {exc}") from exc

        if response.status_code >= 400:
            error = classify_rerank_status(response.status_code)
            if isinstance(error, RerankerPermanentError):
                self._trip_breaker(error)
            raise error

        try:
            body = response.json()
            results = body["results"]
        except Exception as exc:
            raise RerankerProtocolError(
                f"reranker response was not the expected shape: {exc}"
            ) from exc

        ranked = []
        seen: set[int] = set()
        for position, item in enumerate(results, start=1):
            try:
                index = item["index"]
                score = float(item["relevance_score"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RerankerProtocolError(
                    f"reranker result {position} was malformed: {exc}"
                ) from exc
            # bool is an int subclass, and True would index position 1.
            if not isinstance(index, int) or isinstance(index, bool):
                raise RerankerProtocolError(
                    f"reranker returned a non-integer index {index!r}"
                )
            if not 0 <= index < len(sent):
                # A provider returning an out-of-range index would
                # otherwise attach a score to the wrong chunk.
                raise RerankerProtocolError(
                    f"reranker returned index {index} out of range"
                )
            if index in seen:
                # Two scores for one chunk. map_to_parents dedupes by
                # parent so the damage would be bounded, but the repeat
                # still consumes a top_n slot and drops a real candidate.
                raise RerankerProtocolError(f"reranker returned index {index} twice")
            seen.add(index)
            ranked.append(
                RankedCandidate(candidate=sent[index], score=score, rank=position)
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

    Every failure lands here, so the exception type cannot change what
    the user receives -- only what the log says. That is the point: the
    level each class is logged at is the only thing standing between a
    permanently dead reranker and nobody noticing for a month.
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
        # An empty result from a reranker that did not raise means it
        # ranked nothing -- every candidate was blank, or it returned an
        # empty list. RRF order is better than no context at all, and the
        # flag has to say so, since the gate must not read an empty list
        # as calibrated.
        if not ranked:
            logger.warning(
                "reranker %s returned no results; falling back to RRF order",
                reranker.name,
            )
        else:
            return ranked, False
    except RerankerDisabledError:
        # The breaker is open and already logged its ERROR once, when it
        # tripped. Repeating it per query would bury the cause under
        # thousands of copies of its own consequence.
        logger.debug("reranker %s is disabled; using RRF order", reranker.name)
    except RerankerPermanentError as exc:
        # ERROR, not warning, and said once -- the breaker makes every
        # later call raise RerankerDisabledError, which logs at DEBUG.
        # Silent permanent degradation is the failure this path exists to
        # prevent: reranking stops, the confidence gate opens (because
        # uncalibrated scores cannot be thresholded), and every answer
        # afterwards rests on raw RRF order with nothing in the logs
        # connecting the two.
        logger.error(
            "reranker %s disabled for the life of this process: %s. "
            "Answers continue on RRF order, and the confidence gate is "
            "open because RRF scores are not relevance.",
            reranker.name,
            exc,
        )
    except RerankerProtocolError as exc:
        # Loud: this is either a wrong URL (forever) or a proxy error
        # page (momentary), and we cannot tell which from here.
        logger.error(
            "reranker %s returned an unusable response: %s; "
            "falling back to RRF order",
            reranker.name,
            exc,
        )
    except RerankerTransientError as exc:
        logger.warning(
            "reranker %s failed transiently: %s; falling back to RRF order",
            reranker.name,
            exc,
        )
    except asyncio.TimeoutError:
        # Our own wait_for budget, distinct from a timeout the provider
        # reported. asyncio.TimeoutError is the builtin TimeoutError in
        # 3.11+, and httpx's timeouts are not subclasses of it -- those
        # arrive as RerankerTimeoutError above.
        logger.warning(
            "reranker %s timed out after %.1fs; falling back to RRF order",
            reranker.name,
            timeout,
        )
    except Exception:
        # Still here deliberately. A reranker implementation we did not
        # write (or a bug in one we did) must degrade, not 500.
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
    vector_floor: float | None = None,
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

    CONFIDENCE_THRESHOLD is RERANKER-SPECIFIC and now MEASURED, not
    guessed: 0.4162 for Cohere rerank-v3.5, chosen by a rule fixed in
    advance (the midpoint of the widest gap below the lowest answerable
    score) over 24 labelled cases. See docs/eval-baseline.md. It is
    still provisional at n=24, and it is NOT transferable -- a Jina or
    Voyage score of 0.4162 means something else, and a local
    cross-encoder's logit is not even on the same scale.

    What the measurement also established: no threshold separates the
    classes. Two should-refuse questions score ABOVE every answerable
    one, because a cross-encoder measures RELEVANCE and those chunks are
    maximally relevant to a question they do not answer. The gate is a
    cheap filter, not a correctness guarantee; what actually declines
    those two is the grounding instruction in the prompt.
    """
    if not ranked:
        return False

    # Uncalibrated scores cannot be compared to the threshold at all. RRF
    # scores top out around 1/(k+1) ~= 0.016, so testing them against
    # 0.4162 would gate EVERY question -- the system would answer
    # nothing.
    if used_fallback or not reranker.scores_are_calibrated:
        # But "cannot use the reranker score" is not "have no signal".
        # Cosine similarity from the vector leg is unaffected by the
        # reranker being down, and it is calibrated -- measured over the
        # same 24 cases as the threshold above. Before this, a dead
        # reranker meant the gate opened unconditionally and the system
        # answered everything, which is the worst behaviour available
        # during exactly the incident when nobody is watching quality.
        #
        # The floor is deliberately weaker than the reranker threshold:
        # cosine separates the classes worse (8 false positives vs 3 at
        # full recall), so this is damage limitation during an outage,
        # not an equivalent substitute.
        floor = (
            settings.VECTOR_SIMILARITY_FLOOR
            if vector_floor is None
            else vector_floor
        )
        if floor <= 0:
            # 0 disables it, preserving the old answer-everything
            # behaviour for anyone who has not calibrated a floor. An
            # uncalibrated floor applied by default would refuse real
            # questions on a number nobody measured.
            return True
        best = max(
            (
                c.candidate.vector_similarity
                for c in ranked
                if c.candidate.vector_similarity is not None
            ),
            default=None,
        )
        if best is None:
            # Nothing in the ranking came from the vector leg -- a
            # keyword-only match. No calibrated signal exists, so fall
            # back to the permissive reading rather than refusing on an
            # absence of evidence.
            return True
        return best >= floor

    threshold = settings.CONFIDENCE_THRESHOLD if threshold is None else threshold
    return ranked[0].score >= threshold


def check_reranker_config() -> str | None:
    """Warn about the silent half of a reranker misconfiguration.

    get_reranker() already fails loudly the obvious way round:
    RERANKER_PROVIDER=hosted with no key raises at startup. The inverse
    is silent and far likelier -- someone pastes a key into .env and
    never flips the provider, so the cross-encoder never runs, answers
    quietly rest on RRF order, and the confidence gate stays open
    because RRF scores are not relevance. Nothing raises, nothing logs,
    and the only symptom is answers that are a bit worse than they
    should be.

    Returns the message (for tests) and logs it. Not fatal: passthrough
    is a legitimate configuration, so this is a "you probably did not
    mean this", not an error.
    """
    if settings.RERANKER_API_KEY and settings.RERANKER_PROVIDER != "hosted":
        message = (
            f"a reranker API key is configured but RERANKER_PROVIDER is "
            f"'{settings.RERANKER_PROVIDER}', so the key is unused and no "
            f"reranking will happen. Set RERANKER_PROVIDER=hosted to use it."
        )
        logger.warning(message)
        return message
    if settings.RERANKER_PROVIDER == "passthrough":
        logger.info(
            "reranker is passthrough: results keep RRF order and the "
            "confidence gate stays open, since RRF scores are not relevance."
        )
    return None


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

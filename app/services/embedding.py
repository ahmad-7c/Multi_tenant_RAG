"""Texts -> vectors, behind a provider interface.

Two things make this its own module:

  - Tests must never hit the network, so there is a deterministic fake
    implementation with the same contract as the real one.
  - Retry policy is subtle enough to deserve isolating: only the FAILED
    batch is retried, and only for errors that could plausibly succeed on
    a second attempt.
"""
import hashlib
import logging
import math
import random
import time
from abc import ABC, abstractmethod
from enum import Enum
from functools import lru_cache
from typing import Callable

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmbeddingTask(str, Enum):
    """What a text is being embedded FOR.

    Retrieval is asymmetric: a stored passage is long, declarative and
    self-contained, while a question is short and interrogative. A model
    that embeds both identically has to place "Receipts must be submitted
    within thirty days" and "how long do I have to file a receipt?" near
    each other despite them sharing almost no surface form. Models that
    accept a task type are trained with two projections precisely so the
    question lands near its ANSWER rather than near other questions.

    Values are the Gemini API's enum spellings. Providers that have no
    such concept (OpenAI, the fake) ignore the argument.
    """

    DOCUMENT = "RETRIEVAL_DOCUMENT"
    QUERY = "RETRIEVAL_QUERY"


class EmbeddingError(Exception):
    pass


class TransientEmbeddingError(EmbeddingError):
    """Worth retrying: rate limit, 5xx, timeout, connection reset."""


class PermanentEmbeddingError(EmbeddingError):
    """Never retried.

    A 400 means the request itself is wrong -- input too long, bad model
    name, malformed payload. Retrying it burns the budget and delays the
    failure the operator needs to see.
    """


class EmbeddingProvider(ABC):
    @property
    @abstractmethod
    def model(self) -> str: ...

    @property
    @abstractmethod
    def dimensions(self) -> int: ...

    @abstractmethod
    def embed(
        self, texts: list[str], *, task: EmbeddingTask = EmbeddingTask.DOCUMENT
    ) -> list[list[float]]:
        """Embed one batch. Raises Transient/PermanentEmbeddingError.

        `task` defaults to DOCUMENT rather than being required because
        document embedding outnumbers query embedding by thousands to one,
        and a required argument on 20+ call sites buys no safety where the
        provider ignores it anyway. The single dangerous call site -- the
        query path -- is pinned by a test instead.
        """


# Status codes that can succeed on a retry. 408/409 are included because
# some S3-compatible and proxy layers surface a timeout that way.
_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def classify_error(exc: BaseException) -> EmbeddingError:
    """Map a provider exception onto our retry policy.

    Reads the status code where one exists rather than matching on
    exception class names, because every SDK names its exceptions
    differently and renames them between versions.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)

    if isinstance(status, int):
        if status in _TRANSIENT_STATUS:
            return TransientEmbeddingError(f"transient provider error {status}: {exc}")
        if 400 <= status < 500:
            return PermanentEmbeddingError(f"permanent provider error {status}: {exc}")
        if status >= 500:
            return TransientEmbeddingError(f"transient provider error {status}: {exc}")

    # No status at all: a timeout, DNS failure or dropped connection. Those
    # are transient by nature -- the request never reached a decision.
    #
    # "connect" rather than "connection": httpx.ConnectError lowercases to
    # "connecterror", which does NOT contain "connection". With the longer
    # spelling alone, a DNS failure or a refused connection fell through to
    # the permanent branch below and the retry loop silently never ran.
    name = type(exc).__name__.lower()
    if any(
        hint in name
        for hint in ("timeout", "connect", "network", "unavailable", "ratelimit", "protocol")
    ):
        return TransientEmbeddingError(f"transient network error: {exc}")

    # Unknown shape. Treated as permanent on purpose: retrying something we
    # do not understand three times, then failing anyway, just delays the
    # error and triples the cost.
    return PermanentEmbeddingError(f"unclassified embedding error: {exc}")


class OpenAIEmbeddings(EmbeddingProvider):
    def __init__(self, *, api_key: str, model: str, dimensions: int, client=None):
        self._model = model
        self._dimensions = dimensions
        if client is not None:
            self._client = client
        else:
            # Imported lazily so a deployment using the fake provider does
            # not need the SDK or an API key present at import time.
            from openai import OpenAI

            self._client = OpenAI(api_key=api_key)

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(
        self, texts: list[str], *, task: EmbeddingTask = EmbeddingTask.DOCUMENT
    ) -> list[list[float]]:
        # OpenAI's embedding models have no task-type concept: the same
        # projection is used for documents and queries. The argument is
        # accepted and ignored rather than rejected, so the call sites stay
        # provider-agnostic.
        try:
            response = self._client.embeddings.create(model=self._model, input=texts)
        except Exception as exc:
            raise classify_error(exc) from exc

        vectors = [item.embedding for item in response.data]
        if len(vectors) != len(texts):
            # The provider is contractually required to return one vector
            # per input in order. If it does not, a silent zip() would pair
            # vectors with the wrong chunks -- corrupting retrieval in a way
            # no test would catch later.
            raise PermanentEmbeddingError(
                f"provider returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        return vectors


class RateLimiter:
    """A client-side floor on the spacing between requests.

    Not a replacement for retry-on-429 -- the two solve different
    problems. Retry recovers AFTER the provider has already refused and
    (on a free tier) possibly counted the refusal against the quota. The
    throttle stops the refusal happening: a 400-chunk document is 7 batch
    calls fired as fast as the loop can issue them, which trips a
    per-minute limit instantly and then spends the backoff window doing
    nothing. Spacing them costs the same wall-clock time without the
    failures.

    Deliberately NOT a token bucket. A bucket allows a burst up to its
    capacity, which is exactly the behaviour that trips a per-minute cap
    at the start of an ingestion. A flat minimum interval is less
    efficient and much harder to get wrong.

    `clock`/`sleep` are injected so the tests can exercise this without
    actually sleeping.
    """

    def __init__(
        self,
        *,
        requests_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._min_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def acquire(self) -> None:
        if self._min_interval <= 0:
            # 0 means "no throttle". Useful for paid tiers and for tests.
            return
        now = self._clock()
        if self._last is not None:
            wait = self._min_interval - (now - self._last)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last = now


class GeminiEmbeddings(EmbeddingProvider):
    """Google Gemini embeddings over the REST API.

    Uses gemini-embedding-001 rather than the newer gemini-embedding-2:
    -2 is multimodal and auto-renormalizes, but it DROPPED the taskType
    parameter, and asymmetric document/query embedding is worth more to a
    text-only RAG system than multimodality it does not use.

    Talks REST through httpx instead of the google-genai SDK for two
    reasons: httpx is already a dependency, and httpx.HTTPStatusError
    exposes `.response.status_code`, which is what classify_error() reads.
    An SDK with a different exception surface would silently degrade every
    retryable error into a permanent one.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int,
        base_url: str,
        timeout: float,
        requests_per_minute: int,
        client=None,
        limiter: "RateLimiter | None" = None,
    ):
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._limiter = limiter or RateLimiter(requests_per_minute=requests_per_minute)

    def __repr__(self) -> str:
        # Explicit, because the default repr of an object holding
        # self._api_key is one logger.exception() away from printing the
        # key into a log aggregator.
        return f"GeminiEmbeddings(model={self._model!r}, dimensions={self._dimensions})"

    __str__ = __repr__

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def _url(self) -> str:
        return f"{self._base_url}/models/{self._model}:batchEmbedContents"

    def embed(
        self, texts: list[str], *, task: EmbeddingTask = EmbeddingTask.DOCUMENT
    ) -> list[list[float]]:
        if not texts:
            return []

        payload = {
            "requests": [
                {
                    # The per-request `model` is required by
                    # batchEmbedContents even though the URL also names it.
                    "model": f"models/{self._model}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": task.value,
                    "outputDimensionality": self._dimensions,
                }
                for text in texts
            ]
        }
        # Header auth, not ?key=. A query parameter lands in proxy logs,
        # access logs and any exception that records the URL.
        headers = {"x-goog-api-key": self._api_key}

        # Per HTTP request, not per text: one batch call is one unit of
        # free-tier request quota however many texts it carries.
        self._limiter.acquire()

        try:
            if self._client is not None:
                response = self._client.post(self._url, json=payload, headers=headers)
            else:
                # The process-wide pooled client, not a fresh one per
                # batch. A client per call is a connection pool per call:
                # a 400-chunk document paid a TLS handshake on every one
                # of its 7 batches. Timeout rides the REQUEST so the pool
                # stays shared with providers on other budgets.
                from app.core.http import get_sync_client

                response = get_sync_client().post(
                    self._url, json=payload, headers=headers, timeout=self._timeout
                )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            # classify_error stringifies the exception. httpx keeps the key
            # in the headers, not the URL or the message, so this is safe --
            # the test test_gemini_never_puts_the_api_key_in_an_error_message
            # is what keeps it that way.
            raise classify_error(exc) from exc

        return self._vectors_from(body, expected=len(texts))

    def _vectors_from(self, body: object, *, expected: int) -> list[list[float]]:
        if not isinstance(body, dict) or not isinstance(body.get("embeddings"), list):
            raise PermanentEmbeddingError(
                "gemini response missing an 'embeddings' array; "
                "the API contract may have changed"
            )
        items = body["embeddings"]

        if len(items) != expected:
            # A silent zip() would pair vectors with the wrong chunks and
            # corrupt retrieval in a way nothing downstream could detect.
            raise PermanentEmbeddingError(
                f"provider returned {len(items)} vectors for {expected} inputs"
            )

        vectors = []
        for index, item in enumerate(items):
            values = item.get("values") if isinstance(item, dict) else None
            if not isinstance(values, list):
                raise PermanentEmbeddingError(
                    f"gemini response item {index} has no 'values' array"
                )
            if len(values) != self._dimensions:
                # Requested width is not guaranteed width. pgvector would
                # also reject this, but as an opaque DataError raised deep
                # inside a Celery task; naming it here names the cause.
                raise PermanentEmbeddingError(
                    f"gemini returned {len(values)} dimensions, expected "
                    f"{self._dimensions}; check EMBEDDING_DIMENSIONS against "
                    f"what {self._model} supports"
                )
            vectors.append(_l2_normalize(values, model=self._model))
        return vectors


def _l2_normalize(values: list[float], *, model: str) -> list[float]:
    """Scale a vector to unit length.

    gemini-embedding-001 only normalizes its full 3072-dimension output.
    Below that it Matryoshka-truncates, and the prefix of a unit vector is
    not itself unit length. Cosine distance happens to be magnitude-
    invariant, so this is not what makes retrieval work today -- it is
    what stops it breaking the moment anything compares these vectors by
    inner product or Euclidean distance instead, which `vector_ip_ops` and
    `vector_l2_ops` both would.
    """
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        # Dividing would raise, or produce NaNs that poison every distance
        # computed against this row afterwards.
        raise PermanentEmbeddingError(
            f"{model} returned an all-zero vector, which cannot be normalized"
        )
    return [v / norm for v in values]


class EmbeddingModelMismatch(RuntimeError):
    """Stored vectors were produced by a model other than the configured one."""


def check_embedding_model_consistency(db, *, configured_model: str) -> None:
    """Refuse to serve if the corpus was embedded by a different model.

    A vector only means anything relative to the model that produced it.
    Two models embed the same sentence into two UNRELATED regions of
    R^1536 -- not "similar but different", unrelated, the way two hash
    functions give unrelated digests of one string. Cosine distance still
    returns a number for every pair, so nothing raises. The corpus simply
    stops being searchable, retrieval returns the least-bad noise, and the
    confidence gate cannot catch it because the gate is calibrated against
    the RERANKER's scores, not the embedder's. The symptom is "our RAG
    isn't very good", which is indistinguishable from a tuning problem and
    gets debugged for weeks.

    Deliberately NOT org-scoped, unlike every other query here: this is an
    operator-level deployment check, and a stale corpus in org B is still
    a broken deployment when org A asks a question.
    """
    from sqlalchemy import text as sql_text

    rows = db.execute(
        sql_text(
            """
            SELECT DISTINCT embedding_model
            FROM document_chunks
            WHERE embedding IS NOT NULL AND embedding_model IS NOT NULL
            """
        )
    ).all()

    stale = sorted({row[0] for row in rows} - {configured_model})
    if not stale:
        return

    raise EmbeddingModelMismatch(
        f"stored chunks were embedded with {', '.join(stale)} but "
        f"EMBEDDING_MODEL is {configured_model}. Vectors from different "
        f"models are not comparable, so queries would return noise. "
        f"Either set EMBEDDING_MODEL back, or re-ingest every document "
        f"with the new model."
    )


class FakeEmbeddings(EmbeddingProvider):
    """Deterministic embeddings derived from the text itself.

    Deterministic matters twice over: the same text always yields the same
    vector (so a re-ingestion test can assert stability), and similar texts
    do NOT accidentally land near each other, so a retrieval test that
    passes is passing because of the code under test rather than luck.
    """

    def __init__(self, *, dimensions: int | None = None, model: str = "fake-embedding"):
        self._dimensions = dimensions or settings.EMBEDDING_DIMENSIONS
        self._model = model
        self.call_count = 0
        self.batch_sizes: list[int] = []
        self.tasks: list[EmbeddingTask] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(
        self, texts: list[str], *, task: EmbeddingTask = EmbeddingTask.DOCUMENT
    ) -> list[list[float]]:
        self.call_count += 1
        self.batch_sizes.append(len(texts))
        # Recorded, not applied: a fake that produced different vectors per
        # task would break every retrieval test, which embeds its fixture
        # text and its probe text through the same code path on purpose.
        self.tasks.append(task)
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        # A hash seeds a PRNG, so the vector is a pure function of the text
        # and stable across processes and Python versions.
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)
        raw = [rng.gauss(0.0, 1.0) for _ in range(self._dimensions)]
        # L2-normalized so cosine distance behaves the way it does with
        # real embeddings.
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]


def embed_in_batches(
    provider: EmbeddingProvider,
    texts: list[str],
    *,
    batch_size: int | None = None,
    max_retries: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    task: EmbeddingTask = EmbeddingTask.DOCUMENT,
) -> list[list[float]]:
    """Embed every text, retrying only the batch that failed.

    Retrying the whole document (what a Celery-level retry does) re-pays
    for every batch that already succeeded. On a 400-batch document whose
    batch 4 hit a 503, that is 399 wasted calls per attempt.
    """
    batch_size = batch_size or settings.EMBED_BATCH_SIZE
    max_retries = settings.EMBED_MAX_RETRIES if max_retries is None else max_retries

    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors.extend(
            _embed_one_batch(
                provider,
                batch,
                max_retries=max_retries,
                sleep=sleep,
                offset=start,
                task=task,
            )
        )
    return vectors


def _embed_one_batch(
    provider: EmbeddingProvider,
    batch: list[str],
    *,
    max_retries: int,
    sleep: Callable[[float], None],
    offset: int,
    task: EmbeddingTask = EmbeddingTask.DOCUMENT,
) -> list[list[float]]:
    attempt = 0
    while True:
        try:
            return provider.embed(batch, task=task)
        except PermanentEmbeddingError:
            # Never retried: the request is wrong, not unlucky.
            raise
        except TransientEmbeddingError as exc:
            if attempt >= max_retries:
                raise
            delay = settings.EMBED_RETRY_BASE_SECONDS * (2**attempt)
            logger.warning(
                "embedding batch at offset %s failed (%s); retry %s/%s in %.1fs",
                offset,
                exc,
                attempt + 1,
                max_retries,
                delay,
            )
            sleep(delay)
            attempt += 1


@lru_cache(maxsize=1)
def get_embedding_provider() -> EmbeddingProvider:
    provider = settings.EMBEDDING_PROVIDER
    if provider == "fake":
        return FakeEmbeddings(dimensions=settings.EMBEDDING_DIMENSIONS)
    if provider == "gemini":
        if not settings.GEMINI_API_KEY:
            # Loud at startup rather than on the first ingestion, which
            # would mark a document `failed` for a config problem.
            raise RuntimeError("EMBEDDING_PROVIDER=gemini requires GEMINI_API_KEY")
        return GeminiEmbeddings(
            api_key=settings.GEMINI_API_KEY,
            model=settings.EMBEDDING_MODEL,
            dimensions=settings.EMBEDDING_DIMENSIONS,
            base_url=settings.GEMINI_BASE_URL,
            timeout=settings.EMBED_TIMEOUT_SECONDS,
            requests_per_minute=settings.EMBED_REQUESTS_PER_MINUTE,
        )
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY")
    return OpenAIEmbeddings(
        api_key=settings.OPENAI_API_KEY,
        model=settings.EMBEDDING_MODEL,
        dimensions=settings.EMBEDDING_DIMENSIONS,
    )

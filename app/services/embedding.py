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
from functools import lru_cache
from typing import Callable

from app.core.config import settings

logger = logging.getLogger(__name__)


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
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed one batch. Raises Transient/PermanentEmbeddingError."""


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
    name = type(exc).__name__.lower()
    if any(hint in name for hint in ("timeout", "connection", "unavailable", "ratelimit")):
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

    def embed(self, texts: list[str]) -> list[list[float]]:
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

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        self.batch_sizes.append(len(texts))
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
                provider, batch, max_retries=max_retries, sleep=sleep, offset=start
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
) -> list[list[float]]:
    attempt = 0
    while True:
        try:
            return provider.embed(batch)
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
    if settings.EMBEDDING_PROVIDER == "fake":
        return FakeEmbeddings(dimensions=settings.EMBEDDING_DIMENSIONS)
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY")
    return OpenAIEmbeddings(
        api_key=settings.OPENAI_API_KEY,
        model=settings.EMBEDDING_MODEL,
        dimensions=settings.EMBEDDING_DIMENSIONS,
    )

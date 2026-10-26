"""Embedder tests.

Two things are being pinned down: the fake is genuinely deterministic (so
every other test in the suite can rely on it), and the retry policy retries
exactly the batch that failed, exactly for the errors that can succeed on a
second attempt.
"""
import math

import pytest

from app.core.config import settings
from app.services.embedding import (
    FakeEmbeddings,
    OpenAIEmbeddings,
    PermanentEmbeddingError,
    TransientEmbeddingError,
    classify_error,
    embed_in_batches,
)


# ------------------------------------------------------------------ fake ----


def test_fake_embeddings_are_deterministic():
    a = FakeEmbeddings(dimensions=16)
    b = FakeEmbeddings(dimensions=16)

    # Same text -> same vector, across instances. Re-ingestion tests
    # depend on this.
    assert a.embed(["hello world"]) == b.embed(["hello world"])


def test_fake_embeddings_differ_for_different_text():
    provider = FakeEmbeddings(dimensions=16)

    [first], [second] = provider.embed(["alpha"]), provider.embed(["beta"])

    assert first != second


def test_fake_embeddings_are_unit_length():
    [vector] = FakeEmbeddings(dimensions=64).embed(["some text"])

    # Normalized so cosine distance behaves as it does with real
    # embeddings; an unnormalized fake would make distance assertions
    # meaningless.
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


def test_fake_embeddings_have_the_configured_dimension():
    [vector] = FakeEmbeddings().embed(["text"])
    assert len(vector) == settings.EMBEDDING_DIMENSIONS


# -------------------------------------------------------------- batching ----


def test_texts_are_embedded_in_batches_of_the_configured_size():
    provider = FakeEmbeddings(dimensions=8)

    vectors = embed_in_batches(provider, [f"text {i}" for i in range(25)], batch_size=10)

    assert len(vectors) == 25
    assert provider.batch_sizes == [10, 10, 5]


def test_vectors_come_back_in_input_order():
    provider = FakeEmbeddings(dimensions=8)
    texts = [f"text {i}" for i in range(7)]

    vectors = embed_in_batches(provider, texts, batch_size=3)

    # Order is load-bearing: the task zips vectors against chunks, so a
    # reordering would attach every embedding to the wrong chunk.
    for text, vector in zip(texts, vectors):
        assert vector == provider._vector(text)


def test_empty_input_makes_no_calls():
    provider = FakeEmbeddings(dimensions=8)

    assert embed_in_batches(provider, [], batch_size=10) == []
    assert provider.call_count == 0


# --------------------------------------------------------- classification ----


class _Err(Exception):
    def __init__(self, status):
        super().__init__(f"status {status}")
        self.status_code = status


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504, 529])
def test_retryable_statuses_are_transient(status):
    assert isinstance(classify_error(_Err(status)), TransientEmbeddingError)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_client_error_statuses_are_permanent(status):
    # A 400 means the request is wrong, not unlucky. Retrying it burns the
    # budget and delays the error the operator needs to see.
    assert isinstance(classify_error(_Err(status)), PermanentEmbeddingError)


def test_timeout_without_a_status_is_transient():
    class APITimeoutError(Exception):
        pass

    assert isinstance(classify_error(APITimeoutError("timed out")), TransientEmbeddingError)


def test_connection_error_without_a_status_is_transient():
    class APIConnectionError(Exception):
        pass

    assert isinstance(classify_error(APIConnectionError("reset")), TransientEmbeddingError)


def test_unknown_error_is_permanent():
    # Retrying something we do not understand three times and then failing
    # anyway only delays the error and triples the cost.
    assert isinstance(classify_error(ValueError("???")), PermanentEmbeddingError)


# --------------------------------------------------------------- retries ----


class _FlakyProvider(FakeEmbeddings):
    """Fails the Nth batch `fail_times` times, then succeeds."""

    def __init__(self, *, fail_batch_index: int, fail_times: int, error):
        super().__init__(dimensions=8)
        self._fail_batch_index = fail_batch_index
        self._remaining = fail_times
        self._error = error
        self.batch_index = -1
        self.attempted_batches: list[tuple[str, ...]] = []

    def embed(self, texts, **kwargs):
        self.attempted_batches.append(tuple(texts))
        if tuple(texts) not in {tuple(b) for b in self.attempted_batches[:-1]}:
            self.batch_index += 1
        if self.batch_index == self._fail_batch_index and self._remaining > 0:
            self._remaining -= 1
            raise self._error
        return super().embed(texts, **kwargs)


def test_only_the_failed_batch_is_retried():
    """The whole point of batch-level retry.

    A Celery-level retry would re-embed every batch that already
    succeeded. On a document with batch 4 of 10 failing, that is 9 wasted
    calls per attempt.
    """
    provider = _FlakyProvider(
        fail_batch_index=1, fail_times=1, error=TransientEmbeddingError("503")
    )
    sleeps: list[float] = []

    vectors = embed_in_batches(
        provider,
        [f"t{i}" for i in range(9)],
        batch_size=3,
        max_retries=3,
        sleep=sleeps.append,
    )

    assert len(vectors) == 9
    # Batches 0 and 2 were attempted once each; batch 1 twice.
    counts = {}
    for batch in provider.attempted_batches:
        counts[batch] = counts.get(batch, 0) + 1
    assert sorted(counts.values()) == [1, 1, 2]
    assert len(sleeps) == 1


def test_backoff_is_exponential():
    provider = _FlakyProvider(
        fail_batch_index=0, fail_times=3, error=TransientEmbeddingError("429")
    )
    sleeps: list[float] = []

    embed_in_batches(
        provider, ["a", "b"], batch_size=2, max_retries=3, sleep=sleeps.append
    )

    base = settings.EMBED_RETRY_BASE_SECONDS
    assert sleeps == [base, base * 2, base * 4]


def test_permanent_error_is_never_retried():
    provider = _FlakyProvider(
        fail_batch_index=0, fail_times=99, error=PermanentEmbeddingError("400")
    )
    sleeps: list[float] = []

    with pytest.raises(PermanentEmbeddingError):
        embed_in_batches(
            provider, ["a"], batch_size=1, max_retries=3, sleep=sleeps.append
        )

    assert sleeps == [], "a 400 must not cost any backoff time"
    assert len(provider.attempted_batches) == 1


def test_transient_error_gives_up_after_max_retries():
    provider = _FlakyProvider(
        fail_batch_index=0, fail_times=99, error=TransientEmbeddingError("503")
    )

    with pytest.raises(TransientEmbeddingError):
        embed_in_batches(provider, ["a"], batch_size=1, max_retries=2, sleep=lambda _: None)

    # Initial attempt plus two retries.
    assert len(provider.attempted_batches) == 3


# ---------------------------------------------------------------- openai ----


class _FakeOpenAIClient:
    def __init__(self, vectors):
        self._vectors = vectors
        self.embeddings = self

    def create(self, *, model, input):
        class Item:
            def __init__(self, embedding):
                self.embedding = embedding

        class Response:
            def __init__(self, data):
                self.data = data

        return Response([Item(v) for v in self._vectors])


def test_openai_provider_reports_the_configured_model():
    provider = OpenAIEmbeddings(
        api_key="x",
        model="text-embedding-3-large",
        dimensions=3072,
        client=_FakeOpenAIClient([[0.1, 0.2]]),
    )

    # The model name is recorded on every chunk row, so it must come from
    # config rather than being hardcoded anywhere.
    assert provider.model == "text-embedding-3-large"
    assert provider.dimensions == 3072


def test_openai_provider_rejects_a_vector_count_mismatch():
    provider = OpenAIEmbeddings(
        api_key="x",
        model="m",
        dimensions=2,
        client=_FakeOpenAIClient([[0.1, 0.2]]),
    )

    # A silent zip() would pair vectors with the wrong chunks and corrupt
    # retrieval in a way nothing downstream could detect.
    with pytest.raises(PermanentEmbeddingError):
        provider.embed(["one", "two"])

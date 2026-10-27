"""Gemini embedding provider tests.

Everything here runs through an injected fake HTTP client. That pins the
REQUEST we build and our handling of the RESPONSE shape -- the two halves
of the wire contract. What it deliberately cannot prove is that Google
still accepts this shape; only the live smoke test does that.
"""
import math

import pytest

from app.services.embedding import (
    EmbeddingError,
    EmbeddingTask,
    GeminiEmbeddings,
    PermanentEmbeddingError,
    RateLimiter,
    TransientEmbeddingError,
    classify_error,
)

SECRET = "super-secret-key-value"


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "https://example.invalid")
            raise httpx.HTTPStatusError(
                "status " + str(self.status_code),
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )

    def json(self):
        return self._payload


class _FakeHttpClient:
    def __init__(self, payload=None, status=200, raises=None):
        self._payload = payload if payload is not None else {}
        self._status = status
        self._raises = raises
        self.requests: list[dict] = []

    def post(self, url, *, json, headers, timeout=None):
        self.requests.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        if self._raises is not None:
            raise self._raises
        return _FakeResponse(self._payload, self._status)


def _payload(*vectors):
    return {"embeddings": [{"values": list(v)} for v in vectors]}


def _unit(length, seed=1.0):
    raw = [seed + i for i in range(length)]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]


def _gemini(client, *, dimensions=4, model="gemini-embedding-001"):
    return GeminiEmbeddings(
        api_key=SECRET,
        model=model,
        dimensions=dimensions,
        base_url="https://generativelanguage.googleapis.com/v1beta",
        timeout=5.0,
        requests_per_minute=0,  # throttle off; it has its own tests below
        client=client,
    )


# ------------------------------------------------------------- request ------


def test_gemini_builds_the_batch_request_from_config():
    client = _FakeHttpClient(_payload(_unit(4), _unit(4, 2.0)))
    provider = _gemini(client, dimensions=4, model="gemini-embedding-001")

    provider.embed(["alpha", "beta"])

    request = client.requests[0]
    # Endpoint and model are config, never hardcoded downstream.
    assert request["url"] == (
        "https://generativelanguage.googleapis.com/v1beta"
        "/models/gemini-embedding-001:batchEmbedContents"
    )
    body = request["json"]
    assert [r["content"]["parts"][0]["text"] for r in body["requests"]] == ["alpha", "beta"]
    assert all(r["model"] == "models/gemini-embedding-001" for r in body["requests"])
    assert all(r["outputDimensionality"] == 4 for r in body["requests"])


def test_gemini_authenticates_with_a_header_not_a_query_string():
    client = _FakeHttpClient(_payload(_unit(4)))
    provider = _gemini(client)

    provider.embed(["alpha"])

    request = client.requests[0]
    # Google documents the x-goog-api-key header. A ?key= query parameter
    # also works, and also lands in every proxy log, load-balancer access
    # log and exception message that happens to record a URL.
    assert request["headers"]["x-goog-api-key"] == SECRET
    assert SECRET not in request["url"]


def test_gemini_embeds_documents_as_documents():
    client = _FakeHttpClient(_payload(_unit(4)))

    _gemini(client).embed(["a stored chunk"], task=EmbeddingTask.DOCUMENT)

    assert client.requests[0]["json"]["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"


def test_gemini_embeds_questions_as_queries():
    client = _FakeHttpClient(_payload(_unit(4)))

    _gemini(client).embed(["what is the policy?"], task=EmbeddingTask.QUERY)

    # Asymmetric task types are why this uses -001 rather than -2: the
    # model places a short question and a long passage in the same region
    # of the space only when it is told which is which.
    assert client.requests[0]["json"]["requests"][0]["taskType"] == "RETRIEVAL_QUERY"


def test_gemini_defaults_to_the_document_task():
    client = _FakeHttpClient(_payload(_unit(4)))

    _gemini(client).embed(["a stored chunk"])

    assert client.requests[0]["json"]["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"


# ------------------------------------------------------------ response ------


def test_gemini_fails_loudly_on_a_dimension_mismatch():
    # 768 values where 1536 were configured. pgvector would also reject
    # this, but as an opaque DataError deep inside a Celery task; failing
    # here names the actual cause.
    client = _FakeHttpClient(_payload(_unit(768)))
    provider = _gemini(client, dimensions=1536)

    with pytest.raises(PermanentEmbeddingError) as excinfo:
        provider.embed(["alpha"])

    assert "1536" in str(excinfo.value)
    assert "768" in str(excinfo.value)


def test_gemini_fails_loudly_on_a_vector_count_mismatch():
    client = _FakeHttpClient(_payload(_unit(4)))
    provider = _gemini(client, dimensions=4)

    # A silent zip() would pair vectors with the wrong chunks and corrupt
    # retrieval in a way nothing downstream could detect.
    with pytest.raises(PermanentEmbeddingError):
        provider.embed(["alpha", "beta"])


def test_gemini_normalizes_truncated_vectors():
    # gemini-embedding-001 does NOT renormalize below 3072 dimensions --
    # it Matryoshka-truncates, and the prefix of a unit vector is shorter
    # than unit length.
    client = _FakeHttpClient({"embeddings": [{"values": [3.0, 4.0, 0.0, 0.0]}]})
    provider = _gemini(client, dimensions=4)

    [vector] = provider.embed(["alpha"])

    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)
    assert math.isclose(vector[0], 0.6, rel_tol=1e-9)


def test_gemini_rejects_a_zero_vector_rather_than_dividing_by_zero():
    client = _FakeHttpClient({"embeddings": [{"values": [0.0, 0.0, 0.0, 0.0]}]})
    provider = _gemini(client, dimensions=4)

    # Normalizing a zero vector is either a ZeroDivisionError or a row of
    # NaNs that poisons every distance computed against it afterwards.
    with pytest.raises(PermanentEmbeddingError):
        provider.embed(["alpha"])


def test_gemini_rejects_a_malformed_response_body():
    client = _FakeHttpClient({"unexpected": "shape"})
    provider = _gemini(client, dimensions=4)

    with pytest.raises(PermanentEmbeddingError):
        provider.embed(["alpha"])


def test_gemini_reports_the_configured_model_for_the_chunk_row():
    provider = _gemini(_FakeHttpClient(_payload(_unit(4))), model="gemini-embedding-001")

    # Written to document_chunks.embedding_model on every child row.
    assert provider.model == "gemini-embedding-001"
    assert provider.dimensions == 4


# -------------------------------------------------------------- errors ------


def test_gemini_maps_429_to_a_transient_error():
    provider = _gemini(_FakeHttpClient(status=429))

    # Free-tier quota exhaustion. Retried with backoff, never failed.
    with pytest.raises(TransientEmbeddingError):
        provider.embed(["alpha"])


def test_gemini_maps_503_to_a_transient_error():
    provider = _gemini(_FakeHttpClient(status=503))

    with pytest.raises(TransientEmbeddingError):
        provider.embed(["alpha"])


def test_gemini_maps_400_to_a_permanent_error():
    provider = _gemini(_FakeHttpClient(status=400))

    # A malformed request never succeeds on a retry; retrying burns free
    # quota and delays the error the operator needs to see.
    with pytest.raises(PermanentEmbeddingError):
        provider.embed(["alpha"])


def test_gemini_never_puts_the_api_key_in_an_error_message():
    import httpx

    client = _FakeHttpClient(raises=httpx.ConnectError("name resolution failed"))
    provider = _gemini(client)

    with pytest.raises(EmbeddingError) as excinfo:
        provider.embed(["alpha"])

    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(provider)
    assert SECRET not in str(provider)


def test_connect_error_is_transient():
    import httpx

    # "connecterror" does not contain the substring "connection". Before
    # this was fixed a DNS failure or refused connection classified as
    # PERMANENT, so the retry loop silently never ran.
    assert isinstance(classify_error(httpx.ConnectError("boom")), TransientEmbeddingError)


def test_read_timeout_is_transient():
    import httpx

    assert isinstance(classify_error(httpx.ReadTimeout("boom")), TransientEmbeddingError)


# ------------------------------------------------------------ throttle ------


def test_throttle_spaces_requests_to_the_configured_rate():
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    # 60 requests/minute == one per second.
    limiter = RateLimiter(requests_per_minute=60, clock=lambda: now[0], sleep=sleep)

    limiter.acquire()  # first call is free
    limiter.acquire()  # a full second behind the previous one
    limiter.acquire()

    assert slept == [pytest.approx(1.0), pytest.approx(1.0)]


def test_throttle_does_not_sleep_when_calls_are_already_spread_out():
    now = [0.0]
    slept: list[float] = []
    limiter = RateLimiter(
        requests_per_minute=60, clock=lambda: now[0], sleep=lambda s: slept.append(s)
    )

    limiter.acquire()
    now[0] += 10.0
    limiter.acquire()

    # The throttle is a floor on spacing, not a fixed delay: work that is
    # already slow enough must not be slowed further.
    assert slept == []


def test_throttle_is_disabled_when_rate_is_zero():
    slept: list[float] = []
    limiter = RateLimiter(
        requests_per_minute=0, clock=lambda: 0.0, sleep=lambda s: slept.append(s)
    )

    for _ in range(5):
        limiter.acquire()

    assert slept == []


def test_gemini_acquires_the_throttle_before_each_request():
    calls = []

    class _CountingLimiter:
        def acquire(self):
            calls.append(1)

    client = _FakeHttpClient(_payload(_unit(4)))
    provider = GeminiEmbeddings(
        api_key=SECRET,
        model="gemini-embedding-001",
        dimensions=4,
        base_url="https://x/v1beta",
        timeout=5.0,
        requests_per_minute=0,
        client=client,
        limiter=_CountingLimiter(),
    )

    provider.embed(["alpha"])
    provider.embed(["beta"])

    # Throttling is per HTTP request, not per text: one batchEmbedContents
    # call is one unit of free-tier quota regardless of how many texts it
    # carries.
    assert len(calls) == 2

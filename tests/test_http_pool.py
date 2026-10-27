"""The shared HTTP connection pool.

What these tests pin is reuse and lifecycle, not throughput. "Pooling is
faster" is not a property a unit test can assert honestly -- it depends
on the network. What it CAN assert is the thing that makes pooling work
at all: the same client object comes back across calls, it is not closed
underneath other callers, and a client is never shared across event
loops.
"""
import asyncio

import httpx
import pytest

from app.core import http as pooled
from app.core.config import settings


@pytest.fixture(autouse=True)
def _clean_pools():
    """Each test starts with no pooled clients and leaves none behind."""
    pooled.reset_sync_client()
    pooled._async_clients.clear()
    yield
    pooled.close_sync_client()
    pooled._async_clients.clear()


# ---- sync pool (the Celery worker) -----------------------------------


def test_the_sync_client_is_reused_across_calls():
    first = pooled.get_sync_client()
    second = pooled.get_sync_client()

    # The whole point. A new client per call is a new connection pool per
    # call, which means a TLS handshake per embedding batch.
    assert first is second


def test_the_sync_client_is_rebuilt_after_being_closed():
    first = pooled.get_sync_client()
    pooled.close_sync_client()

    second = pooled.get_sync_client()

    # A closed client raises on use instead of reconnecting, so handing
    # one back out would break every later request in the process.
    assert second is not first
    assert not second.is_closed


def test_reset_does_not_close_the_inherited_sockets():
    client = pooled.get_sync_client()

    pooled.reset_sync_client()

    # reset_ is what a forked child runs, where the fds belong to the
    # parent too. Closing them would tear down the PARENT's connections.
    assert not client.is_closed
    assert pooled.get_sync_client() is not client


def test_the_sync_pool_carries_the_configured_limits():
    client = pooled.get_sync_client()

    # Reading httpx internals on purpose: the limits are the reason this
    # module exists, and a silently-ignored Limits object would leave the
    # pool unbounded with every other test here still passing.
    transport = client._transport
    assert transport._pool._max_connections == settings.HTTP_MAX_CONNECTIONS
    assert (
        transport._pool._max_keepalive_connections
        == settings.HTTP_MAX_KEEPALIVE_CONNECTIONS
    )
    assert transport._pool._keepalive_expiry == settings.HTTP_KEEPALIVE_EXPIRY_SECONDS


def test_the_default_timeout_is_never_unbounded():
    client = pooled.get_sync_client()

    # None means "wait forever" in httpx. A caller that forgets a
    # per-request timeout should get a slow failure, not a stuck worker.
    assert client.timeout.read is not None
    assert client.timeout.connect == settings.HTTP_CONNECT_TIMEOUT_SECONDS


# ---- async pool (the API process) ------------------------------------


@pytest.mark.asyncio
async def test_the_async_client_is_reused_within_one_loop():
    first = pooled.get_async_client()
    second = pooled.get_async_client()

    assert first is second


@pytest.mark.asyncio
async def test_the_async_client_is_rebuilt_after_close():
    first = pooled.get_async_client()
    await pooled.aclose_async_client()

    second = pooled.get_async_client()

    assert second is not first
    assert not second.is_closed


@pytest.mark.asyncio
async def test_closing_twice_is_harmless():
    pooled.get_async_client()
    await pooled.aclose_async_client()

    # The lifespan runs this on shutdown; a second call (a test teardown,
    # a re-entered lifespan) must not raise.
    await pooled.aclose_async_client()


def test_each_event_loop_gets_its_own_async_client():
    """The reason the registry is keyed by loop rather than a global.

    An AsyncClient's pooled connections belong to the loop that opened
    them. One module-level client reused across loops raises "Event loop
    is closed" the second time -- which in production, where there is one
    loop forever, would never happen, and in the test suite, where
    pytest-asyncio builds a loop per test, would happen constantly.
    """
    clients = []

    async def grab():
        clients.append(pooled.get_async_client())

    asyncio.run(grab())
    asyncio.run(grab())

    assert len(clients) == 2
    assert clients[0] is not clients[1]


def test_a_dead_loops_client_is_not_handed_to_a_new_loop():
    # WeakKeyDictionary rather than id(loop) -> client: CPython reuses
    # id() after an object is collected, so an id-keyed registry can hand
    # a brand new loop a pool full of connections belonging to a closed
    # one. Nothing in production would notice until a request failed.
    async def grab():
        return pooled.get_async_client()

    first = asyncio.run(grab())
    second = asyncio.run(grab())

    assert first is not second


# ---- the providers actually use it -----------------------------------


@pytest.mark.asyncio
async def test_the_reranker_uses_the_pool_and_does_not_close_it(monkeypatch):
    import uuid

    from app.services.reranker import HostedReranker
    from app.services.retrieval import Candidate

    sent = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"results": [{"index": 0, "relevance_score": 0.7}]}

    async def fake_post(url, *, json, headers, timeout=None):
        sent["timeout"] = timeout
        return _Resp()

    client = pooled.get_async_client()
    monkeypatch.setattr(client, "post", fake_post)

    reranker = HostedReranker(
        api_key="k", model="m", base_url="https://x/rerank", timeout=3.0
    )
    candidate = Candidate(
        chunk_id=uuid.uuid4(),
        parent_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="d.pdf",
        page_number=1,
        content="body",
        token_count=1,
        rrf_score=0.016,
        vector_rank=1,
        text_rank=None,
    )

    await reranker.rerank("q", [candidate], 1)

    # The 3s budget must ride the REQUEST. On the client it would either
    # fragment the pool by timeout value or be overridden by the pool's
    # 30s backstop.
    assert sent["timeout"] == 3.0
    # And the pool must survive: `async with httpx.AsyncClient(...)`
    # here would close the connections the LLM is streaming over.
    assert not client.is_closed
    assert pooled.get_async_client() is client


def test_the_embedder_uses_the_pool_and_does_not_close_it(monkeypatch):
    from app.services.embedding import GeminiEmbeddings

    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            # Non-zero: an all-zero vector cannot be L2-normalized and
            # the embedder rightly refuses it.
            return {"embeddings": [{"values": [0.1] * settings.EMBEDDING_DIMENSIONS}]}

    def fake_post(url, *, json, headers, timeout=None):
        sent["timeout"] = timeout
        return _Resp()

    client = pooled.get_sync_client()
    monkeypatch.setattr(client, "post", fake_post)

    embedder = GeminiEmbeddings(
        api_key="k",
        model=settings.EMBEDDING_MODEL,
        dimensions=settings.EMBEDDING_DIMENSIONS,
        base_url=settings.GEMINI_BASE_URL,
        timeout=settings.EMBED_TIMEOUT_SECONDS,
        requests_per_minute=0,
    )
    embedder.embed(["hello"])

    assert sent["timeout"] == settings.EMBED_TIMEOUT_SECONDS
    # `with httpx.Client(...)` per batch was the old behaviour: it threw
    # away a warm connection after every batch of 64 texts.
    assert not client.is_closed
    assert pooled.get_sync_client() is client

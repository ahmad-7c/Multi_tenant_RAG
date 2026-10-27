"""Shared, pooled HTTP clients for the provider calls.

Every provider was building its own client per call:

    with httpx.Client(timeout=...) as client:      # embedding, per batch
    httpx.AsyncClient(timeout=...)                 # llm, per answer
    async with httpx.AsyncClient(timeout=...):     # reranker, per query

A client owns a connection pool, so a client per call is a pool per
call, which is no pool at all: each one opens a fresh TCP connection and
runs a fresh TLS handshake, then throws the warm connection away. That
is roughly 2-3 round trips to the provider before a single byte of
payload moves. One user question can make four such calls (query
embedding, rewrite, rerank, answer) and a 400-chunk document makes one
per embedding batch.

Two pools, not one, because there are two execution contexts:

  * the API process is async     -> httpx.AsyncClient (LLM, reranker)
  * the Celery worker is sync    -> httpx.Client      (embeddings)

Timeouts stay per-provider. The reranker gets 3 seconds and the LLM gets
30, so a client-level timeout would force a separate pool per timeout
value -- which would put us back where we started. Instead every caller
passes `timeout=` on the request itself, and the client-level value is
only a backstop for a caller that forgets.
"""
import asyncio
import threading
import weakref
from typing import Optional

import httpx

from app.core.config import settings


def _limits() -> httpx.Limits:
    """Pool size.

    Small on purpose. The pool caps how many provider requests can be in
    flight at once, and on a free tier the per-minute request quota binds
    long before the socket count does -- so a large pool buys nothing and
    only widens the blast radius of a provider that has started hanging.

    `keepalive_expiry` matters more than the sizes. A pooled connection
    that the provider's load balancer has already dropped is worse than
    no pooled connection: the next request spends its budget discovering
    the socket is dead. Expiring ours first keeps that from being the
    user's problem.
    """
    return httpx.Limits(
        max_connections=settings.HTTP_MAX_CONNECTIONS,
        max_keepalive_connections=settings.HTTP_MAX_KEEPALIVE_CONNECTIONS,
        keepalive_expiry=settings.HTTP_KEEPALIVE_EXPIRY_SECONDS,
    )


def _default_timeout() -> httpx.Timeout:
    # Connect is separate from read: a connect that hangs is a dead
    # endpoint and should fail fast, while a slow read is a model
    # thinking. Read is the backstop for a caller that passes no
    # per-request timeout -- never None, which in httpx means "wait
    # forever" and would turn one bad request into a stuck worker.
    return httpx.Timeout(
        settings.HTTP_DEFAULT_TIMEOUT_SECONDS,
        connect=settings.HTTP_CONNECT_TIMEOUT_SECONDS,
    )


# ---- async (the API process) -----------------------------------------

# Keyed by the running event loop, NOT a bare module global. An
# AsyncClient's pooled connections belong to the loop that opened them,
# so one global client reused across loops raises "Event loop is closed"
# or "attached to a different loop". In production there is one loop
# forever and a global would look perfect; under pytest-asyncio there is
# a new loop per test, so the bug would only ever appear in the test
# suite. A WeakKeyDictionary rather than id(loop) -> client because
# id() is reused after a loop is collected, which would hand a brand new
# loop a client full of connections belonging to a dead one.
_async_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary()
)


def get_async_client() -> httpx.AsyncClient:
    """The pooled AsyncClient for the running loop, created on first use.

    No lock. Construction is synchronous, so there is no await between
    the lookup and the store, and a single-threaded event loop cannot
    interleave two coroutines inside that window.
    """
    loop = asyncio.get_running_loop()
    client = _async_clients.get(loop)
    # is_closed matters: aclose_async_client() may have run (lifespan
    # shutdown, or a test tearing down), and a closed client raises on
    # use rather than reconnecting.
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=_default_timeout(), limits=_limits())
        _async_clients[loop] = client
    return client


async def aclose_async_client() -> None:
    """Close the running loop's client. Safe to call when there is none."""
    loop = asyncio.get_running_loop()
    client = _async_clients.pop(loop, None)
    if client is not None and not client.is_closed:
        await client.aclose()


# ---- sync (the Celery worker) ----------------------------------------

_sync_client: Optional[httpx.Client] = None
# A real lock here, unlike the async side: Celery's thread and gevent
# pools run tasks concurrently in one process, so two threads can reach
# the None check together and the loser's client would leak unclosed.
_sync_lock = threading.Lock()


def get_sync_client() -> httpx.Client:
    """The process-wide pooled Client, created on first use.

    Created lazily rather than at import on purpose. Celery's prefork
    pool forks AFTER importing the task module, and a connection pool
    inherited across fork is shared file descriptors: two processes
    writing into one TLS socket corrupt each other's streams. Building
    it on first use means each child builds its own -- and
    reset_sync_client() on worker_process_init drops any that a parent
    opened before forking.
    """
    global _sync_client
    with _sync_lock:
        if _sync_client is None or _sync_client.is_closed:
            _sync_client = httpx.Client(timeout=_default_timeout(), limits=_limits())
        return _sync_client


def reset_sync_client() -> None:
    """Drop the pooled sync client without closing its sockets.

    Deliberately NOT close(). This runs in a freshly forked child, where
    the file descriptors belong to the parent as well -- closing them
    here would shut down connections the parent is still using. Dropping
    the reference leaks the inherited fds in this child until exit,
    which is the cheap half of the trade.
    """
    global _sync_client
    with _sync_lock:
        _sync_client = None


def close_sync_client() -> None:
    """Close the pooled sync client. For a clean process shutdown."""
    global _sync_client
    with _sync_lock:
        if _sync_client is not None and not _sync_client.is_closed:
            _sync_client.close()
        _sync_client = None

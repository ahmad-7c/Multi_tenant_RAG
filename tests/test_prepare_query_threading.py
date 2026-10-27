"""prepare_query must do no blocking work on the event loop.

Three of its steps block: the query embedding (a synchronous httpx POST,
preceded by RateLimiter's time.sleep) and the two database calls. All
three have to be dispatched through run_blocking, because this coroutine
runs in the API process where one event loop serves every request --
blocking it does not slow down the one question being asked, it freezes
every other user's stream mid-token.

The first test pins the dispatch, deterministically and without threads.
The second one demonstrates the consequence with real threads, because
"it is dispatched through a callable" is the mechanism, not the point:
the point is that the loop keeps running.
"""
import asyncio
import threading
import time
import uuid

import pytest

from app.core.config import settings
from app.services import query_service
from app.services.cache import InMemoryAnswerCache
from app.services.embedding import EmbeddingTask, FakeEmbeddings
from app.services.llm import FakeLLM
from app.services.reranker import FakeReranker

pytestmark = pytest.mark.asyncio


class _StopBeforeRetrieval(Exception):
    """Ends prepare_query after the embedding, before it needs a database.

    Lets these tests exercise the real threading path without a session,
    since retrieval is the next step and would need one.
    """


def _kwargs(**overrides):
    kwargs = dict(
        session_factory=lambda: None,
        org_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        question="who approves expenses",
        history=[],
        index_version=1,
        embedder=FakeEmbeddings(dimensions=settings.EMBEDDING_DIMENSIONS),
        reranker=FakeReranker(),
        rewrite_llm=FakeLLM(answer="rewritten"),
        cache=InMemoryAnswerCache(),
    )
    kwargs.update(overrides)
    return kwargs


async def test_the_query_embedding_is_dispatched_through_run_blocking():
    """Deterministic, no threads: was embed() called inside run_blocking?

    A flag set only for the duration of a run_blocking call, read by the
    embedder itself. If the embedding is ever moved back inline, the
    flag is False and this fails -- which the two-fake test below cannot
    detect, since a fake embedder returns instantly either way.
    """
    inside_run_blocking = False
    observed: list[bool] = []

    async def recording_run_blocking(fn):
        nonlocal inside_run_blocking
        inside_run_blocking = True
        try:
            return fn()
        finally:
            inside_run_blocking = False

    class _Spy(FakeEmbeddings):
        def embed(self, texts, *, task=EmbeddingTask.DOCUMENT):
            observed.append(inside_run_blocking)
            raise _StopBeforeRetrieval()

    with pytest.raises(_StopBeforeRetrieval):
        await query_service.prepare_query(
            **_kwargs(
                embedder=_Spy(dimensions=settings.EMBEDDING_DIMENSIONS),
                run_blocking=recording_run_blocking,
            )
        )

    assert observed == [True]


async def test_the_query_embedding_runs_on_a_worker_thread():
    """With the production run_blocking, embed() is off the loop thread."""
    loop_thread = threading.current_thread()
    ran_on: list[threading.Thread] = []

    class _Spy(FakeEmbeddings):
        def embed(self, texts, *, task=EmbeddingTask.DOCUMENT):
            ran_on.append(threading.current_thread())
            raise _StopBeforeRetrieval()

    with pytest.raises(_StopBeforeRetrieval):
        await query_service.prepare_query(
            **_kwargs(
                embedder=_Spy(dimensions=settings.EMBEDDING_DIMENSIONS),
                run_blocking=query_service._to_thread,
            )
        )

    assert ran_on and ran_on[0] is not loop_thread


async def test_a_slow_embedder_does_not_freeze_the_event_loop():
    """The consequence, with real threads and a real block.

    A ticker counts how many times the loop got control while the
    embedder sat in time.sleep for 300ms. Threaded, it ticks freely.
    Inline on the loop -- the behaviour before this step -- it could not
    tick at all, because nothing else runs while the loop is blocked.
    """
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    class _Slow(FakeEmbeddings):
        def embed(self, texts, *, task=EmbeddingTask.DOCUMENT):
            # A real blocking call, not asyncio.sleep: the whole failure
            # mode is that embed() does not yield to the loop.
            time.sleep(0.3)
            raise _StopBeforeRetrieval()

    task = asyncio.create_task(ticker())
    try:
        with pytest.raises(_StopBeforeRetrieval):
            await query_service.prepare_query(
                **_kwargs(
                    embedder=_Slow(dimensions=settings.EMBEDDING_DIMENSIONS),
                    run_blocking=query_service._to_thread,
                )
            )
    finally:
        task.cancel()

    # 300ms of block against a 10ms tick is ~30 ticks if the loop is
    # free. The floor is deliberately far below that -- this asserts
    # "the loop was alive", not a scheduling rate, so a loaded CI box
    # cannot make it flap. Inline, the honest expectation is 0.
    assert ticks >= 5


async def test_the_query_task_type_survives_the_threading():
    """QUERY, not the DOCUMENT default, still reaches the embedder.

    Worth re-pinning here: the task type is now passed inside a closure
    dispatched to another thread, which is exactly the kind of move that
    quietly drops a keyword argument. Getting it wrong raises nothing --
    retrieval just gets quietly worse.
    """
    embedder = FakeEmbeddings(dimensions=settings.EMBEDDING_DIMENSIONS)

    class _Stop(FakeReranker):
        async def rerank(self, query, candidates, top_n):
            raise _StopBeforeRetrieval()

    async def run_blocking_or_stop(fn):
        result = fn()
        if embedder.tasks:
            # Past the embedding; the next dispatch is retrieval, which
            # needs a database this test does not have.
            raise _StopBeforeRetrieval()
        return result

    with pytest.raises(_StopBeforeRetrieval):
        await query_service.prepare_query(
            **_kwargs(embedder=embedder, run_blocking=run_blocking_or_stop)
        )

    assert embedder.tasks == [EmbeddingTask.QUERY]

"""The concurrent-stream cap, and releasing the slot on every exit.

The cap itself is simple. Releasing it is where the bugs are, and all
three exit paths are tested here because each one leaks differently:

  * normal completion -- the easy case,
  * an exception in generation or persistence -- leaks if the release
    sits after the `yield` loop instead of in a `finally`,
  * CLIENT DISCONNECT -- leaks even with a `finally` around the wrong
    thing, because the cancellation propagates out of the `await` and
    any release line placed after it never runs.

A leaked slot is not fatal: Redis members older than
STREAM_SLOT_TTL_SECONDS are reclaimed by the next acquire. But it costs
a tenant one of three slots for three minutes, repeatedly, for as long
as users close tabs -- which they do constantly on a streaming answer
they have finished reading.
"""
import uuid

import pytest

from app.api import limits
from app.core.config import settings
from app.models.conversation import Conversation
from app.services.cache import InMemoryAnswerCache
from app.services.embedding import FakeEmbeddings
from app.services.llm import FakeLLM, LLMRateLimitError
from app.services.rate_limit import InMemoryLimiter
from app.services.reranker import FakeReranker


class CountingLimiter(InMemoryLimiter):
    """Records acquire/release so a leak is visible, not inferred."""

    def __init__(self):
        super().__init__()
        self.acquired: list[str] = []
        self.released: list[str] = []

    async def acquire_stream_slot(self, org_id, *, limit):
        slot = await super().acquire_stream_slot(org_id, limit=limit)
        if slot is not None:
            self.acquired.append(slot)
        return slot

    async def release_stream_slot(self, org_id, slot):
        self.released.append(slot)
        await super().release_stream_slot(org_id, slot)

    def in_use(self, org_id) -> int:
        return len(self._streams.get(str(org_id), {}))


@pytest.fixture()
def wired(client, db_session, monkeypatch, org_factory, user_factory):
    """A conversation plus a corpus, with every provider faked."""
    import app.api.v1.routes.conversations as route
    from app.models.document import Document, DocumentStatus
    from sqlalchemy import text as sql_text

    org = org_factory()
    user = user_factory(org)
    document = Document(
        id=uuid.uuid4(),
        org_id=org.id,
        uploaded_by=user.id,
        filename="policy.md",
        storage_key="p.md",
        file_size_bytes=10,
        status=DocumentStatus.READY,
    )
    db_session.add(document)
    db_session.flush()

    parent_id = uuid.uuid4()
    child_id = uuid.uuid4()
    for cid, pid, content in (
        (parent_id, None, "Expense reports are due within thirty days."),
        (child_id, parent_id, "Expense reports are due within thirty days."),
    ):
        db_session.execute(
            sql_text(
                """
                INSERT INTO document_chunks
                    (id, org_id, document_id, parent_id, content, embedding,
                     embedding_model, page_number, token_count, chunk_index)
                VALUES (:id, :org, :doc, :parent, :content,
                        CAST(:embedding AS vector), :model, 1, 8, 0)
                """
            ),
            {
                "id": cid,
                "org": org.id,
                "doc": document.id,
                "parent": pid,
                "content": content,
                "embedding": "[" + ",".join(["0.02"] * settings.EMBEDDING_DIMENSIONS) + "]",
                "model": settings.EMBEDDING_MODEL,
            },
        )

    conversation = Conversation(
        id=uuid.uuid4(), org_id=org.id, user_id=user.id, title="t"
    )
    db_session.add(conversation)
    db_session.commit()

    monkeypatch.setattr(route, "get_llm", lambda: FakeLLM(answer="Thirty days [1]."))
    monkeypatch.setattr(route, "get_reranker", lambda: FakeReranker())
    monkeypatch.setattr(
        route,
        "get_embedding_provider",
        lambda: FakeEmbeddings(dimensions=settings.EMBEDDING_DIMENSIONS),
    )
    monkeypatch.setattr(route, "get_answer_cache", lambda: InMemoryAnswerCache())
    monkeypatch.setattr(route, "get_rewrite_llm", lambda: FakeLLM(answer="unused"))
    monkeypatch.setattr(route, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    limiter = CountingLimiter()
    limits.set_limiter(limiter)
    return org, user, conversation, limiter, route


def _ask(client, user, conversation, auth_headers, content="how long for expenses?"):
    with client.stream(
        "POST",
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"content": content},
        headers=auth_headers(user),
    ) as response:
        return response.status_code, "".join(response.iter_text())


# ------------------------------------------------------------ the cap --


def test_a_fourth_concurrent_stream_is_refused(wired, client, auth_headers):
    org, user, conversation, limiter, _ = wired

    # Three slots held by other in-flight answers for this org.
    for _ in range(settings.MAX_CONCURRENT_STREAMS_PER_ORG):
        assert (
            await_sync(limiter.acquire_stream_slot(str(org.id),
                       limit=settings.MAX_CONCURRENT_STREAMS_PER_ORG))
            is not None
        )

    status, _body = _ask(client, user, conversation, auth_headers)
    assert status == 429


def test_the_cap_is_per_org(wired, client, auth_headers, db_session, org_factory, user_factory):
    org, user, conversation, limiter, _ = wired

    # Another org fills its own three slots.
    other = org_factory()
    for _ in range(settings.MAX_CONCURRENT_STREAMS_PER_ORG):
        await_sync(
            limiter.acquire_stream_slot(
                str(other.id), limit=settings.MAX_CONCURRENT_STREAMS_PER_ORG
            )
        )

    status, _body = _ask(client, user, conversation, auth_headers)
    # One tenant saturating its cap must not stop another from asking.
    assert status == 200


def test_the_slot_refusal_carries_no_retry_after(wired, client, auth_headers):
    org, user, conversation, limiter, _ = wired
    for _ in range(settings.MAX_CONCURRENT_STREAMS_PER_ORG):
        await_sync(
            limiter.acquire_stream_slot(
                str(org.id), limit=settings.MAX_CONCURRENT_STREAMS_PER_ORG
            )
        )

    with client.stream(
        "POST",
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"content": "q"},
        headers=auth_headers(user),
    ) as response:
        assert response.status_code == 429
        # Unlike a token bucket, a concurrency cap has no refill rate to
        # compute a wait from: it clears when somebody else's answer
        # finishes, which is unknowable here. A guessed Retry-After
        # would be worse than none.
        assert "Retry-After" not in response.headers


# ------------------------------------------------------- the release --


def test_the_slot_is_released_after_a_normal_answer(wired, client, auth_headers):
    org, user, conversation, limiter, _ = wired

    status, body = _ask(client, user, conversation, auth_headers)
    assert status == 200
    assert "citations" in body

    assert len(limiter.acquired) == 1
    assert limiter.released == limiter.acquired
    assert limiter.in_use(org.id) == 0


def test_two_sequential_answers_do_not_accumulate_slots(wired, client, auth_headers):
    org, user, conversation, limiter, _ = wired
    for _ in range(3):
        _ask(client, user, conversation, auth_headers)
    # Without a release this would be 3 and the fourth question would
    # 429 -- a tenant locked out by their own finished answers.
    assert limiter.in_use(org.id) == 0
    assert len(limiter.released) == 3


def test_the_slot_is_released_when_generation_fails(
    wired, client, auth_headers, monkeypatch
):
    org, user, conversation, limiter, route = wired

    class ExplodingLLM(FakeLLM):
        async def stream(self, **kwargs):
            raise LLMRateLimitError("429 from the provider")
            yield ""  # pragma: no cover - unreachable, keeps it a generator

    monkeypatch.setattr(route, "get_llm", lambda: ExplodingLLM())

    status, body = _ask(client, user, conversation, auth_headers)
    assert status == 200  # the stream opened; the failure is in its body
    assert "busy" in body or "citations" in body
    # Released even though the answer failed. If the release sat after
    # the yield loop instead of in a `finally`, a provider outage would
    # also exhaust every tenant's stream slots.
    assert limiter.in_use(org.id) == 0


def test_the_slot_is_released_when_persistence_fails(
    wired, client, auth_headers, monkeypatch
):
    org, user, conversation, limiter, route = wired
    from app.services import query_service

    def explode(*args, **kwargs):
        raise RuntimeError("the database went away after the answer")

    monkeypatch.setattr(query_service, "persist_turn", explode)

    try:
        _ask(client, user, conversation, auth_headers)
    except BaseException:
        # The exception is allowed to escape, and what it escapes AS is
        # not the point: Starlette runs the stream inside an anyio task
        # group, so a RuntimeError raised in the body surfaces as an
        # ExceptionGroup. Catching the specific type would make this
        # test about Starlette's internals. What is being tested is
        # that the slot did not escape with it.
        pass
    assert limiter.in_use(org.id) == 0
    assert limiter.released == limiter.acquired


def test_the_slot_is_released_when_the_client_disconnects(
    wired, client, auth_headers
):
    """The exit path a naive implementation drops.

    `await event_stream(); release()` looks correct and is not: a client
    closing the tab raises CancelledError out of the await, so the
    release line is never reached. Only a `finally` wrapping the
    iteration survives it.
    """
    org, user, conversation, limiter, _ = wired

    with client.stream(
        "POST",
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"content": "how long for expenses?"},
        headers=auth_headers(user),
    ) as response:
        assert response.status_code == 200
        # Read ONE chunk and walk away, which is what a closed tab
        # looks like on the wire.
        next(response.iter_text())

    assert limiter.in_use(org.id) == 0
    assert limiter.released == limiter.acquired


def test_a_degraded_limiter_serves_without_a_cap(wired, client, auth_headers):
    """Redis down must not stop answers.

    The slot cap fails OPEN, unlike the per-minute bucket. The bucket
    has already failed closed onto a per-process limiter, so spend is
    bounded; refusing here as well would take the product down during a
    Redis incident for a second-order protection.
    """
    from app.services.rate_limit import RedisUnavailable

    org, user, conversation, limiter, _ = wired

    class SlotlessLimiter(CountingLimiter):
        async def acquire_stream_slot(self, org_id, *, limit):
            raise RedisUnavailable("simulated outage")

    limits.set_limiter(SlotlessLimiter())

    status, body = _ask(client, user, conversation, auth_headers)
    assert status == 200
    assert "citations" in body


def await_sync(coro):
    """Run a coroutine from a sync test.

    These tests drive the app through the synchronous TestClient, so
    there is no running loop to await on; the limiter being primed is
    setup, not the thing under test.
    """
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


# ---- the disconnect path, driven the way the SERVER sees it ------------


@pytest.mark.asyncio
async def test_closing_the_body_iterator_still_releases_the_slot(
    wired, auth_headers
):
    """A disconnect, exercised through the ASGI mechanism it really uses.

    `test_the_slot_is_released_when_the_client_disconnects` above does
    NOT catch a leak here, and mutation testing is how that was found:
    moving the release out of the `finally` and onto the line after the
    loop left that test green. TestClient buffers the whole response, so
    abandoning its iterator never cancels anything server-side -- the
    generator had already run to completion before the test stopped
    reading.

    What a real server does when a client vanishes is call `aclose()` on
    the response's body iterator, which raises GeneratorExit at the
    suspended `yield`. A `finally` runs; a statement after the loop does
    not. So this test drives the route's own body_iterator directly:
    take one chunk, then close it.
    """
    from starlette.requests import Request

    from app.main import app as fastapi_app
    from app.schemas.conversation import CreateMessageRequest

    org, user, conversation, limiter, route = wired

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": f"/api/v1/conversations/{conversation.id}/messages",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 1234),
            "app": fastapi_app,
        }
    )

    response = await route.create_message(
        conversation_id=conversation.id,
        payload=CreateMessageRequest(content="how long for expenses?"),
        request=request,
        current_user=user,
    )

    assert len(limiter.acquired) == 1

    iterator = response.body_iterator
    await iterator.__anext__()          # one token reaches the client
    assert limiter.in_use(org.id) == 1  # the slot is genuinely held
    await iterator.aclose()             # the client goes away

    assert limiter.in_use(org.id) == 0
    assert limiter.released == limiter.acquired

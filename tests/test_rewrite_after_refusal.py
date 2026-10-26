"""End to end: a follow-up asked after a safety block.

This is the test the whole `kind` column exists for. It drives the real
SSE endpoint twice on one conversation -- first a question the model
refuses, then an ordinary follow-up -- and asserts the refusal text
never reaches the rewriter.
"""
import json
import uuid

import pytest

from app.core.config import settings
from app.models.conversation import Conversation, Message, MessageKind, MessageRole
from app.services.cache import InMemoryAnswerCache
from app.services.embedding import FakeEmbeddings
from app.services.llm import SAFETY_BLOCKED_ANSWER, FakeLLM, LLMSafetyBlockedError
from app.services.reranker import FakeReranker
from tests.test_conversations import _conversation, corpus, embedder  # noqa: F401


class _RecordingRewriter(FakeLLM):
    """A rewrite model that records exactly what history it was shown."""

    def __init__(self):
        super().__init__(answer="rewritten standalone question")
        self.prompts: list[str] = []

    async def complete(self, *, system, user, max_tokens=None, temperature=None):
        self.prompts.append(user)
        return self._answer


class _Blocked(FakeLLM):
    async def stream(self, **kwargs):
        raise LLMSafetyBlockedError("blocked: SAFETY")
        yield  # pragma: no cover -- makes this an async generator


def _wire(monkeypatch, db_session, *, llm, rewriter, cache):
    import app.api.v1.routes.conversations as route

    monkeypatch.setattr(route, "get_llm", lambda: llm)
    monkeypatch.setattr(route, "get_rewrite_llm", lambda: rewriter)
    monkeypatch.setattr(route, "get_reranker", lambda: FakeReranker())
    monkeypatch.setattr(
        route,
        "get_embedding_provider",
        lambda: FakeEmbeddings(dimensions=settings.EMBEDDING_DIMENSIONS),
    )
    monkeypatch.setattr(route, "get_answer_cache", lambda: cache)
    monkeypatch.setattr(route, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)


def _ask(client, conversation, user, auth_headers, question):
    with client.stream(
        "POST",
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"content": question},
        headers=auth_headers(user),
    ) as response:
        return "".join(response.iter_text())


def test_a_follow_up_after_a_safety_block_is_rewritten_without_the_refusal(
    client, db_session, corpus, auth_headers, monkeypatch  # noqa: F811
):
    org, user, _document, _parents = corpus
    conversation = _conversation(db_session, org, user)
    rewriter = _RecordingRewriter()
    cache = InMemoryAnswerCache()

    # --- turn 1: the model refuses -----------------------------------
    _wire(monkeypatch, db_session, llm=_Blocked(), rewriter=rewriter, cache=cache)
    body = _ask(
        client, conversation, user, auth_headers, "receipts submitted within thirty days"
    )
    citations_block = [b for b in body.split("\n\n") if "event: citations" in b][0]
    assert json.loads(citations_block.split("data: ")[1])["answer"] == SAFETY_BLOCKED_ANSWER

    assistant = (
        db_session.query(Message)
        .filter(
            Message.conversation_id == conversation.id,
            Message.role == MessageRole.ASSISTANT,
        )
        .one()
    )
    # Marked a refusal, and still present for display.
    assert assistant.kind is MessageKind.REFUSAL
    assert assistant.content == SAFETY_BLOCKED_ANSWER

    # --- turn 2: an ordinary follow-up -------------------------------
    rewriter.prompts.clear()
    _wire(
        monkeypatch,
        db_session,
        llm=FakeLLM(answer="Director approval above 500 USD [1]."),
        rewriter=rewriter,
        cache=cache,
    )
    _ask(client, conversation, user, auth_headers, "what about expense claims approval?")

    assert rewriter.prompts, "the rewriter must have run on a follow-up"
    shown = rewriter.prompts[-1]

    # The refusal is absent from what the rewriter saw...
    assert SAFETY_BLOCKED_ANSWER not in shown
    assert "safety filter" not in shown
    # ...while the user's own earlier question is still there, because it
    # is legitimate context for resolving the follow-up.
    assert "receipts submitted within thirty days" in shown


def test_a_follow_up_after_a_real_answer_still_sees_it(
    client, db_session, corpus, auth_headers, monkeypatch  # noqa: F811
):
    """The complement: filtering refusals must not filter answers.

    Without this, a passing refusal test could be satisfied by a filter
    that drops every assistant turn.
    """
    org, user, _document, _parents = corpus
    conversation = _conversation(db_session, org, user)
    rewriter = _RecordingRewriter()
    cache = InMemoryAnswerCache()

    _wire(
        monkeypatch,
        db_session,
        llm=FakeLLM(answer="Receipts must be filed within thirty days [1]."),
        rewriter=rewriter,
        cache=cache,
    )
    _ask(
        client, conversation, user, auth_headers, "receipts submitted within thirty days"
    )

    assistant = (
        db_session.query(Message)
        .filter(
            Message.conversation_id == conversation.id,
            Message.role == MessageRole.ASSISTANT,
        )
        .one()
    )
    assert assistant.kind is MessageKind.ANSWER

    rewriter.prompts.clear()
    _ask(client, conversation, user, auth_headers, "what about expense claims approval?")

    assert "within thirty days" in rewriter.prompts[-1]


def test_a_gated_not_found_is_marked_a_refusal(
    client, db_session, corpus, auth_headers, monkeypatch  # noqa: F811
):
    """The confidence gate produces a refusal too, not an answer.

    Same reasoning as a safety block: "I could not find anything..." is
    not a topic, and feeding it to the rewriter makes the next question
    retrieve against the phrasing of a non-answer.
    """
    org, user, _document, _parents = corpus
    conversation = _conversation(db_session, org, user)
    rewriter = _RecordingRewriter()

    _wire(
        monkeypatch,
        db_session,
        llm=FakeLLM(answer="unused"),
        rewriter=rewriter,
        cache=InMemoryAnswerCache(),
    )
    # Shares no vocabulary with the corpus, so FakeReranker scores it 0
    # and the gate closes before any model call.
    _ask(client, conversation, user, auth_headers, "zebra quantum helicopter")

    assistant = (
        db_session.query(Message)
        .filter(
            Message.conversation_id == conversation.id,
            Message.role == MessageRole.ASSISTANT,
        )
        .one()
    )
    assert assistant.kind is MessageKind.REFUSAL

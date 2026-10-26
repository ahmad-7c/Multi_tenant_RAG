"""Assistant messages carry a kind, and only answers steer the rewriter.

The problem this solves: the query rewriter is given recent turns so it
can resolve "what about contractors?" against what came before. Feeding
it a refusal poisons that. After a safety block the history reads

    User:      <the blocked question>
    Assistant: I can't answer that question. The request was blocked by
               the content safety filter.
    User:      what about contractors?

and the rewriter -- which is a language model doing what it is told --
now has "content safety filter" as the most recent assistant topic. It
produces things like "what about contractors and the content safety
filter?", which then becomes the retrieval query AND the cache key. One
refusal degrades every subsequent turn in the conversation.

The messages still have to be STORED: the user saw them, and a
conversation that silently drops its own turns is lying about what
happened. So they are kept for display and excluded from rewrite.
"""
import uuid

import pytest

from app.models.conversation import Conversation, Message, MessageKind, MessageRole
from app.services import conversation_service


@pytest.fixture()
def conversation(db_session, org_factory, user_factory):
    org = org_factory()
    user = user_factory(org)
    conv = Conversation(id=uuid.uuid4(), org_id=org.id, user_id=user.id, title="t")
    db_session.add(conv)
    db_session.commit()
    return conv


def _add(db, conversation, role, content, kind=None):
    message = Message(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        org_id=conversation.org_id,
        role=role,
        content=content,
        kind=kind,
    )
    db.add(message)
    db.commit()
    return message


# ------------------------------------------------------------- the column --


def test_kind_round_trips_through_postgres(db_session, conversation):
    for kind in MessageKind:
        message = _add(db_session, conversation, MessageRole.ASSISTANT, "x", kind)
        db_session.refresh(message)
        assert message.kind is kind


def test_user_messages_have_no_kind(db_session, conversation):
    # kind describes what the ASSISTANT produced. A user turn is neither
    # an answer nor a refusal, so NULL is the honest value rather than
    # inventing a fourth enum member.
    message = _add(db_session, conversation, MessageRole.USER, "a question")
    db_session.refresh(message)
    assert message.kind is None


# ------------------------------------------------- rewrite history filter --


def test_rewrite_history_keeps_answers(db_session, conversation):
    _add(db_session, conversation, MessageRole.USER, "how long for receipts?")
    _add(
        db_session,
        conversation,
        MessageRole.ASSISTANT,
        "Thirty days [1].",
        MessageKind.ANSWER,
    )

    history = conversation_service.history_for_rewrite(
        db_session, org_id=conversation.org_id, conversation_id=conversation.id, turns=4
    )

    assert [m["content"] for m in history] == ["how long for receipts?", "Thirty days [1]."]


def test_rewrite_history_drops_refusals_but_keeps_the_question(db_session, conversation):
    _add(db_session, conversation, MessageRole.USER, "how long for receipts?")
    _add(
        db_session,
        conversation,
        MessageRole.ASSISTANT,
        "I can't answer that question. The request was blocked by the "
        "content safety filter.",
        MessageKind.REFUSAL,
    )

    history = conversation_service.history_for_rewrite(
        db_session, org_id=conversation.org_id, conversation_id=conversation.id, turns=4
    )

    # The user's own question survives: it is legitimate context for
    # resolving a pronoun in the next turn. Only the refusal goes.
    assert [m["content"] for m in history] == ["how long for receipts?"]
    assert not any("safety filter" in m["content"] for m in history)


def test_rewrite_history_drops_errors(db_session, conversation):
    _add(db_session, conversation, MessageRole.USER, "how long for receipts?")
    _add(
        db_session,
        conversation,
        MessageRole.ASSISTANT,
        "The answering service is busy right now. Please try again in a moment.",
        MessageKind.ERROR,
    )

    history = conversation_service.history_for_rewrite(
        db_session, org_id=conversation.org_id, conversation_id=conversation.id, turns=4
    )

    assert not any("busy" in m["content"] for m in history)


def test_legacy_rows_with_no_kind_are_treated_as_answers(db_session, conversation):
    # Every assistant message written before this column existed is NULL.
    # Excluding them would silently blank the rewrite history of every
    # pre-existing conversation.
    _add(db_session, conversation, MessageRole.ASSISTANT, "Thirty days [1].", None)

    history = conversation_service.history_for_rewrite(
        db_session, org_id=conversation.org_id, conversation_id=conversation.id, turns=4
    )

    assert [m["content"] for m in history] == ["Thirty days [1]."]


def test_the_full_conversation_still_shows_refusals(db_session, conversation):
    """Excluded from rewrite, kept for display.

    Dropping them from the transcript would make the product lie about
    its own history: the user watched that refusal appear.
    """
    _add(db_session, conversation, MessageRole.USER, "q")
    _add(db_session, conversation, MessageRole.ASSISTANT, "refused", MessageKind.REFUSAL)

    messages = conversation_service.list_messages(
        db_session, org_id=conversation.org_id, conversation_id=conversation.id
    )

    assert [m.content for m in messages] == ["q", "refused"]


def test_the_turn_limit_counts_only_usable_turns(db_session, conversation):
    """A run of refusals must not starve the window.

    If the limit were applied before filtering, four refused turns in a
    row would leave the rewriter with an empty history even though real
    answers exist just above them.
    """
    _add(db_session, conversation, MessageRole.USER, "the original question")
    _add(db_session, conversation, MessageRole.ASSISTANT, "the real answer", MessageKind.ANSWER)
    for index in range(4):
        _add(db_session, conversation, MessageRole.USER, f"blocked q{index}")
        _add(
            db_session,
            conversation,
            MessageRole.ASSISTANT,
            f"refusal {index}",
            MessageKind.REFUSAL,
        )

    history = conversation_service.history_for_rewrite(
        db_session, org_id=conversation.org_id, conversation_id=conversation.id, turns=2
    )

    assert not any("refusal" in m["content"] for m in history)
    assert history, "the window must not be emptied by a run of refusals"


# ---------------------------------------------------------- turn ordering --


def test_a_turn_reads_question_then_answer(db_session, conversation):
    """Both rows of a turn are written in ONE transaction.

    Postgres now() is transaction_timestamp(): constant for the whole
    transaction. persist_turn adds the user message and the assistant
    message and commits once, so both land with an IDENTICAL created_at
    and the sort falls through to the tie-break. Sorting on role is not
    a tie-break, it is alphabetical -- and 'assistant' < 'user', so the
    transcript rendered the answer above the question that produced it.
    """
    from app.services import query_service
    from app.services.query_service import PreparedQuery

    prepared = PreparedQuery(
        org_id=conversation.org_id,
        user_id=conversation.user_id,
        conversation_id=conversation.id,
        raw_question="how long for receipts?",
        rewritten_question="how long for receipts?",
        was_rewritten=False,
        index_version=1,
        cache_key="k",
    )
    query_service.persist_turn(
        db_session, prepared=prepared, answer="Thirty days.", citations=[]
    )

    messages = conversation_service.list_messages(
        db_session, org_id=conversation.org_id, conversation_id=conversation.id
    )

    assert [m.role for m in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert [m.content for m in messages] == ["how long for receipts?", "Thirty days."]


def test_rewrite_history_sees_the_turn_in_the_right_order(db_session, conversation):
    from app.services import query_service
    from app.services.query_service import PreparedQuery

    prepared = PreparedQuery(
        org_id=conversation.org_id,
        user_id=conversation.user_id,
        conversation_id=conversation.id,
        raw_question="how long for receipts?",
        rewritten_question="how long for receipts?",
        was_rewritten=False,
        index_version=1,
        cache_key="k",
    )
    query_service.persist_turn(
        db_session, prepared=prepared, answer="Thirty days.", citations=[]
    )

    history = conversation_service.history_for_rewrite(
        db_session, org_id=conversation.org_id, conversation_id=conversation.id, turns=4
    )

    # A rewriter shown "Assistant: Thirty days." BEFORE the question it
    # answers is being handed a scrambled conversation.
    assert [m["role"] for m in history] == ["user", "assistant"]

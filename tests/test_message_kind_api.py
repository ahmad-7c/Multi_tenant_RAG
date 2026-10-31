"""`kind` reaches the client, so a refusal is not displayed as an answer.

The data was already right -- migration 0006 added the column, the
query service writes it, and `list_messages` returns the rows -- but
`MessageResponse` dropped it on the way out. A client reading the
history therefore saw three indistinguishable assistant bubbles:

    "Expense reports are due within thirty days [1]."
    "I could not find anything in your documents about that."
    "The service is busy right now. Please try again in a moment."

Only the first is an answer. The second is a product signal and the
third is an outage, and a UI that cannot tell them apart has to
pattern-match the prose -- which is precisely the guessing `kind`
exists to replace, and which breaks the first time a refusal sentence
is reworded.

NULL is kept as NULL on the wire rather than defaulted to `answer`:
user turns have no kind, and pre-0006 assistant rows have none either.
Reporting an unknown refusal as an answer is a worse lie than
reporting that we do not know.
"""
import uuid

import pytest

from app.models.conversation import Conversation, Message, MessageKind, MessageRole


@pytest.fixture()
def seeded(db_session, org_factory, user_factory):
    org = org_factory()
    user = user_factory(org)
    conv = Conversation(id=uuid.uuid4(), org_id=org.id, user_id=user.id, title="t")
    db_session.add(conv)
    db_session.commit()

    rows = [
        (MessageRole.USER, "how long for expenses?", None),
        (MessageRole.ASSISTANT, "Thirty days [1].", MessageKind.ANSWER),
        (MessageRole.USER, "and motorcycles?", None),
        (MessageRole.ASSISTANT, "Not in your documents.", MessageKind.REFUSAL),
        (MessageRole.USER, "retry", None),
        (MessageRole.ASSISTANT, "The service is busy.", MessageKind.ERROR),
    ]
    for role, content, kind in rows:
        db_session.add(
            Message(
                id=uuid.uuid4(),
                conversation_id=conv.id,
                org_id=org.id,
                role=role,
                content=content,
                kind=kind,
            )
        )
    db_session.commit()
    return org, user, conv


def _messages(client, auth_headers, user, conv):
    response = client.get(
        f"/api/v1/conversations/{conv.id}/messages", headers=auth_headers(user)
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_each_kind_is_visible_on_the_wire(client, auth_headers, seeded):
    _, user, conv = seeded
    body = _messages(client, auth_headers, user, conv)

    assistant = [m for m in body if m["role"] == "assistant"]
    assert [m["kind"] for m in assistant] == ["answer", "refusal", "error"]


def test_a_user_turn_reports_no_kind_rather_than_an_invented_one(
    client, auth_headers, seeded
):
    _, user, conv = seeded
    body = _messages(client, auth_headers, user, conv)

    for message in body:
        if message["role"] == "user":
            # Present as a key and null, not absent: a client that reads
            # `message.kind` must get None, not a KeyError, and the field
            # being optional is a statement about the value, not about
            # whether the API bothers to mention it.
            assert "kind" in message
            assert message["kind"] is None


def test_a_pre_migration_assistant_row_is_unknown_not_an_answer(
    client, auth_headers, db_session, seeded
):
    org, user, conv = seeded
    # What a row written before migration 0006 looks like: an assistant
    # turn with no kind at all. It must not be reported as `answer`.
    db_session.add(
        Message(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            org_id=org.id,
            role=MessageRole.ASSISTANT,
            content="an old answer, or an old refusal -- we do not know",
            kind=None,
        )
    )
    db_session.commit()

    body = _messages(client, auth_headers, user, conv)
    legacy = [m for m in body if m["content"].startswith("an old answer")]
    assert len(legacy) == 1
    assert legacy[0]["kind"] is None

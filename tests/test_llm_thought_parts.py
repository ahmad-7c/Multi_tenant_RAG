"""Reasoning parts must never reach the user or the Message row.

`gemini-3.5-flash-lite` reports `"thinking": true` and rejects
`thinkingBudget: 0` with a 400, so thinking cannot be switched off on
this model -- the only thing standing between the model's private
reasoning and the answer text is whether the client asks for it
(`thinkingConfig.includeThoughts`) and whether it filters what comes
back.

This client never asks, which is why the measured live run saw zero
thought parts and `thoughtsTokenCount` absent. That makes the filter
dead code TODAY and worth testing anyway: the failure it prevents is
not a crash but a silent corruption -- reasoning spliced into a cited
answer, persisted, and cached -- triggered by one future config change
that looks harmless.

So these tests feed frames the live API does not currently send. That
is deliberate: a test that only covered today's traffic would pass
equally well with the filter deleted.
"""
import json

import pytest

from tests.test_llm_gemini import _FakeAsyncClient, _collect, _llm

pytestmark = pytest.mark.asyncio


def _mixed_frame(*parts):
    return "data: " + json.dumps(
        {"candidates": [{"content": {"parts": list(parts), "role": "model"}}]}
    )


async def test_stream_drops_thought_parts():
    client = _FakeAsyncClient(
        lines=[
            _mixed_frame(
                {"text": "The user is asking about expenses. ", "thought": True},
                {"text": "Reports are due within thirty days [1]."},
            )
        ]
    )
    pieces = await _collect(_llm(client))
    assert "".join(pieces) == "Reports are due within thirty days [1]."


async def test_stream_keeps_parts_that_say_they_are_not_thoughts():
    # An explicit false and an absent key both mean "visible". The test
    # in the filter is `is not True` for exactly this reason: a
    # `thought: false` part treated as a thought would delete real
    # answer text, which is the same bug pointing the other way.
    client = _FakeAsyncClient(
        lines=[_mixed_frame({"text": "visible ", "thought": False}, {"text": "too"})]
    )
    assert "".join(await _collect(_llm(client))) == "visible too"


async def test_a_frame_of_only_thought_parts_yields_nothing():
    # It must not yield "" either: an empty piece reaching the route
    # becomes an empty SSE token event, and the stream's own
    # empty-answer guard counts pieces, not characters.
    client = _FakeAsyncClient(
        lines=[
            _mixed_frame({"text": "thinking...", "thought": True}),
            _mixed_frame({"text": "Answer."}),
        ]
    )
    assert await _collect(_llm(client)) == ["Answer."]


async def test_complete_drops_thought_parts():
    # complete() is what the JUDGE and the query rewriter use. Reasoning
    # leaking here is worse than in an answer: the rewriter's output is
    # embedded and searched, and the judge's is parsed as JSON.
    client = _FakeAsyncClient(
        payload={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Let me consider the policy. ", "thought": True},
                            {"text": '{"asserts_answer": false}'},
                        ]
                    }
                }
            ]
        }
    )
    text = await _llm(client).complete(system="sys", user="usr")
    assert text == '{"asserts_answer": false}'


async def test_a_candidate_of_only_thoughts_is_an_empty_response_not_an_answer():
    from app.services.llm import LLMEmptyResponseError

    client = _FakeAsyncClient(
        payload={
            "candidates": [
                {"content": {"parts": [{"text": "reasoning only", "thought": True}]}}
            ]
        }
    )
    # Not "": an empty answer is a distinct, named failure, and
    # returning "" would hand the judge an unparseable verdict and the
    # user a blank bubble.
    with pytest.raises(LLMEmptyResponseError):
        await _llm(client).complete(system="sys", user="usr")


async def test_the_client_still_does_not_request_thoughts():
    # The filter is the second line of defence. The first is never
    # setting includeThoughts, and that is what keeps the token budget
    # spent on the answer. If this assertion ever fails, the filter
    # above stops being dead code and these tests start mattering.
    client = _FakeAsyncClient(lines=[_mixed_frame({"text": "hi"})])
    await _collect(_llm(client))
    payload = client.stream_calls[0]["json"]
    assert "thinkingConfig" not in json.dumps(payload)

"""Deadlines, retries and malformed-frame accounting for the Gemini LLM.

Separate from test_llm_gemini.py because these are about the call's
behaviour over TIME rather than about the wire format.
"""
import asyncio
import logging

import pytest

from app.services.llm import (
    GeminiLLM,
    LLMRateLimitError,
    LLMSafetyBlockedError,
    LLMTimeoutError,
    LLMUpstreamError,
)
from tests.test_llm_gemini import (
    SECRET,
    _FakeAsyncClient,
    _FakeStreamResponse,
    _collect,
    _frame,
    _llm,
)


# ================================================================ deadlines ==
#
# httpx's timeout= is a PER-OPERATION stall limit: it fires when a single
# read waits too long. A model emitting one token every two seconds
# forever never trips it -- each individual read is fast, the call simply
# never ends. A total deadline is a different guarantee and needs its own
# machinery.


class _SlowStreamResponse(_FakeStreamResponse):
    def __init__(self, lines, delay, status=200):
        super().__init__(lines, status)
        self._delay = delay

    async def aiter_lines(self):
        for line in self._lines:
            await asyncio.sleep(self._delay)
            yield line


class _SlowClient(_FakeAsyncClient):
    def __init__(self, lines, delay):
        super().__init__(lines=lines)
        self._delay = delay

    def stream(self, method, url, *, json, headers):
        self.stream_calls.append(
            {"method": method, "url": url, "json": json, "headers": headers}
        )
        return _SlowStreamResponse(self._lines, self._delay)


def _timed(client, *, timeout=30.0, total_timeout=5.0):
    return GeminiLLM(
        api_key=SECRET,
        model="m",
        base_url="https://x/v1beta",
        timeout=timeout,
        total_timeout=total_timeout,
        client=client,
    )


async def test_a_slow_but_never_stalled_stream_still_hits_the_total_deadline():
    # Each individual read is well inside the per-read limit; the call as
    # a whole is not. Only a total deadline catches this.
    client = _SlowClient([_frame("tick") for _ in range(100)], delay=0.02)

    with pytest.raises(LLMTimeoutError) as excinfo:
        await _collect(_timed(client, timeout=30.0, total_timeout=0.1))

    assert "deadline" in str(excinfo.value).lower()


async def test_the_total_deadline_does_not_fire_on_a_fast_stream():
    client = _SlowClient([_frame("one"), _frame(finish_reason="STOP")], delay=0.001)

    assert await _collect(_timed(client, total_timeout=5.0)) == ["one"]


def test_the_per_read_and_total_limits_are_separate_settings():
    from app.core.config import settings

    # Two different guarantees, so two different knobs. One value would
    # force the total deadline to be as long as the slowest acceptable
    # single read, or the read limit as short as the whole call. Neither
    # is what either one means.
    assert settings.LLM_TOTAL_TIMEOUT_SECONDS > settings.LLM_TIMEOUT_SECONDS


# ================================================================== retries ==


class _SequencedClient(_FakeAsyncClient):
    """Returns each queued (lines, status) in turn, one per attempt."""

    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)
        self.attempts = 0

    def stream(self, method, url, *, json, headers):
        self.stream_calls.append(
            {"method": method, "url": url, "json": json, "headers": headers}
        )
        self.attempts += 1
        lines, status = self._responses.pop(0)
        return _FakeStreamResponse(lines, status)


def _retrying(client, *, max_retries=1, retry_base_seconds=0.0, sleep=None):
    return GeminiLLM(
        api_key=SECRET,
        model="m",
        base_url="https://x/v1beta",
        timeout=10.0,
        total_timeout=30.0,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
        client=client,
        sleep=sleep,
    )


async def test_a_429_before_the_first_token_is_retried():
    client = _SequencedClient(
        [([], 429), ([_frame("recovered"), _frame(finish_reason="STOP")], 200)]
    )

    assert await _collect(_retrying(client)) == ["recovered"]
    assert client.attempts == 2


async def test_a_503_before_the_first_token_is_retried():
    client = _SequencedClient(
        [([], 503), ([_frame("ok"), _frame(finish_reason="STOP")], 200)]
    )

    assert await _collect(_retrying(client)) == ["ok"]
    assert client.attempts == 2


async def test_retries_are_bounded_and_the_error_still_surfaces():
    client = _SequencedClient([([], 429), ([], 429)])

    with pytest.raises(LLMRateLimitError):
        await _collect(_retrying(client, max_retries=1))

    # One initial attempt plus one retry -- not an unbounded loop against
    # a provider that is already telling us to back off.
    assert client.attempts == 2


async def test_a_400_is_never_retried():
    client = _SequencedClient([([], 400), ([_frame("unreachable")], 200)])

    with pytest.raises(LLMUpstreamError):
        await _collect(_retrying(client))

    # A malformed request never succeeds on a second attempt; retrying
    # only burns free-tier quota and delays the real error.
    assert client.attempts == 1


async def test_a_safety_block_is_never_retried():
    client = _SequencedClient(
        [([_frame(prompt_block="SAFETY")], 200), ([_frame("unreachable")], 200)]
    )

    with pytest.raises(LLMSafetyBlockedError):
        await _collect(_retrying(client))

    assert client.attempts == 1


async def test_an_empty_response_is_not_retried():
    client = _SequencedClient(
        [
            ([_frame(finish_reason="STOP")], 200),
            ([_frame("unreachable"), _frame(finish_reason="STOP")], 200),
        ]
    )

    with pytest.raises(Exception):
        await _collect(_retrying(client))

    # The call succeeded; the model simply had nothing to say. A retry
    # sends the identical prompt and gets the identical nothing.
    assert client.attempts == 1


async def test_a_failure_after_the_first_token_is_not_retried():
    """The one case where retrying would corrupt the answer.

    Tokens already streamed cannot be unsent. A retry would restart
    generation from scratch, so the client would receive the opening of
    one answer followed by the whole of another.
    """
    client = _SequencedClient(
        [
            ([_frame("partial text"), _frame(finish_reason="SAFETY")], 200),
            ([_frame("unreachable")], 200),
        ]
    )

    with pytest.raises(LLMSafetyBlockedError):
        await _collect(_retrying(client))

    assert client.attempts == 1


async def test_retry_backoff_is_applied_between_attempts():
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    client = _SequencedClient(
        [([], 429), ([_frame("ok"), _frame(finish_reason="STOP")], 200)]
    )

    await _collect(
        _retrying(client, max_retries=1, retry_base_seconds=0.75, sleep=_sleep)
    )

    # Short, because this is in the user's request path -- a long backoff
    # here is indistinguishable from a hang.
    assert slept == [pytest.approx(0.75)]


async def test_backoff_grows_between_successive_retries():
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    client = _SequencedClient([([], 429), ([], 429), ([], 429)])

    with pytest.raises(LLMRateLimitError):
        await _collect(
            _retrying(client, max_retries=2, retry_base_seconds=0.5, sleep=_sleep)
        )

    assert slept == [pytest.approx(0.5), pytest.approx(1.0)]


# ======================================================== malformed frames ===


async def test_malformed_frames_are_counted_and_logged_with_a_request_id(caplog):
    client = _FakeAsyncClient(
        lines=[
            _frame("one"),
            "data: {broken",
            "data: also-not-json",
            _frame("two"),
            "data: {still broken",
            _frame(finish_reason="STOP"),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="app.services.llm"):
        assert await _collect(_llm(client)) == ["one", "two"]

    summary = [r for r in caplog.records if "unparseable" in r.getMessage()]
    assert summary, "a malformed-frame summary must be logged"
    record = summary[-1]

    # ONE summary carrying the count, not one line per bad frame: a
    # provider having a bad minute would otherwise write a log line per
    # chunk and bury everything else in the file.
    assert len(summary) == 1
    assert "3" in record.getMessage()

    # Plus a correlation id, so the count ties back to the single request
    # that produced it rather than to "sometime this hour".
    assert getattr(record, "request_id", None)
    assert record.request_id in record.getMessage()


async def test_no_summary_is_logged_when_every_frame_parses(caplog):
    client = _FakeAsyncClient(lines=[_frame("one"), _frame(finish_reason="STOP")])

    with caplog.at_level(logging.WARNING, logger="app.services.llm"):
        await _collect(_llm(client))

    assert not [r for r in caplog.records if "unparseable" in r.getMessage()]


async def test_the_request_id_differs_across_calls(caplog):
    ids = []
    for _ in range(2):
        caplog.clear()
        client = _FakeAsyncClient(
            lines=[_frame("x"), "data: {bad", _frame(finish_reason="STOP")]
        )
        with caplog.at_level(logging.WARNING, logger="app.services.llm"):
            await _collect(_llm(client))
        ids.append(
            [r for r in caplog.records if "unparseable" in r.getMessage()][-1].request_id
        )

    assert ids[0] != ids[1]


async def test_a_stream_that_is_entirely_malformed_still_reports_empty(caplog):
    from app.services.llm import LLMEmptyResponseError

    client = _FakeAsyncClient(lines=["data: {bad", "data: {also bad"])

    with caplog.at_level(logging.WARNING, logger="app.services.llm"):
        # Not "a successful empty answer": the frames were garbage, and
        # the empty-response error is what gets the user a sentence.
        with pytest.raises(LLMEmptyResponseError):
            await _collect(_llm(client))

    assert [r for r in caplog.records if "unparseable" in r.getMessage()]

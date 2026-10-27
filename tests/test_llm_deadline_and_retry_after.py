"""The total deadline as a REQUEST budget, and Retry-After handling.

Two related ideas:

  - A deadline that resets on each retry is not a deadline. With one
    retry and a 120s "deadline", the worst case is 240s plus backoff --
    and the caller who set 120 has no way to discover that.
  - A 429 often carries Retry-After, which is the provider telling us
    exactly how long to wait. Guessing an exponential backoff when the
    server has already said "4 seconds" is strictly worse; and when it
    says "90 seconds", waiting is worse than failing, because the user
    is sitting in front of a request handler.
"""
import asyncio

import pytest

from app.services.llm import (
    GeminiLLM,
    LLMDeadlineExceededError,
    LLMRateLimitError,
)
from tests.test_llm_gemini import SECRET, _FakeAsyncClient, _FakeStreamResponse, _collect, _frame


class _ScriptedResponse(_FakeStreamResponse):
    """A response whose lines arrive with a delay, optionally with headers."""

    def __init__(self, lines, status=200, delay=0.0, headers=None):
        super().__init__(lines, status)
        self._delay = delay
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "https://example.invalid")
            raise httpx.HTTPStatusError(
                "status " + str(self.status_code),
                request=request,
                response=httpx.Response(
                    self.status_code, request=request, headers=self.headers
                ),
            )

    async def aiter_lines(self):
        for line in self._lines:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield line


class _ScriptedClient(_FakeAsyncClient):
    """Plays a queue of (lines, status, delay, headers) one per attempt."""

    def __init__(self, script):
        super().__init__()
        self._script = list(script)
        self.attempts = 0

    def stream(self, method, url, *, json, headers, timeout=None):
        self.stream_calls.append({"url": url, "json": json, "headers": headers})
        self.attempts += 1
        lines, status, delay, hdrs = self._script.pop(0)
        return _ScriptedResponse(lines, status, delay, hdrs)


def _provider(client, **kwargs):
    kwargs.setdefault("total_timeout", 5.0)
    kwargs.setdefault("max_retries", 1)
    kwargs.setdefault("retry_base_seconds", 0.0)
    return GeminiLLM(
        api_key=SECRET,
        model="m",
        base_url="https://x/v1beta",
        timeout=10.0,
        client=client,
        **kwargs,
    )


# ================================================= deadline spans retries ===


async def test_the_deadline_covers_the_whole_request_not_each_attempt():
    """Attempt 1 fails slowly, attempt 2 would also be slow.

    Each attempt on its own fits inside the budget; the two together do
    not. A per-attempt deadline would let this run for twice the number
    the caller configured.
    """
    slow = [_frame("tick") for _ in range(100)]
    client = _ScriptedClient(
        [
            ([], 429, 0.0, None),
            (slow, 200, 0.02, None),
        ]
    )

    with pytest.raises(LLMDeadlineExceededError):
        await _collect(_provider(client, total_timeout=0.25, retry_base_seconds=0.1))

    # It did retry -- the budget is shared, not per-attempt.
    assert client.attempts == 2


async def test_backoff_time_counts_against_the_deadline():
    client = _ScriptedClient(
        [
            ([], 429, 0.0, None),
            ([_frame("would succeed"), _frame(finish_reason="STOP")], 200, 0.0, None),
        ]
    )

    # The backoff alone exceeds what is left of the budget, so the retry
    # is abandoned BEFORE sleeping rather than after.
    with pytest.raises(LLMDeadlineExceededError):
        await _collect(_provider(client, total_timeout=0.1, retry_base_seconds=10.0))

    assert client.attempts == 1


async def test_a_deadline_expiry_is_never_retried():
    slow = [_frame("tick") for _ in range(100)]
    client = _ScriptedClient([(slow, 200, 0.02, None), (slow, 200, 0.0, None)])

    with pytest.raises(LLMDeadlineExceededError):
        await _collect(_provider(client, total_timeout=0.1, max_retries=3))

    # Retrying a blown deadline is incoherent: there is no budget left to
    # retry inside of.
    assert client.attempts == 1


async def test_a_deadline_error_is_a_timeout_error_for_the_user_message():
    from app.services.llm import TIMEOUT_ANSWER, LLMTimeoutError, safe_answer_for

    # Subclassing LLMTimeoutError keeps the user-facing mapping correct
    # while still being distinguishable for the retry decision.
    assert issubclass(LLMDeadlineExceededError, LLMTimeoutError)
    assert safe_answer_for(LLMDeadlineExceededError("x")) == TIMEOUT_ANSWER


async def test_a_fast_request_is_unaffected_by_the_shared_deadline():
    client = _ScriptedClient(
        [
            ([], 429, 0.0, None),
            ([_frame("recovered"), _frame(finish_reason="STOP")], 200, 0.0, None),
        ]
    )

    assert await _collect(_provider(client, total_timeout=10.0)) == ["recovered"]
    assert client.attempts == 2


# ============================================================ Retry-After ===


async def test_a_short_retry_after_is_honoured_instead_of_the_backoff():
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    client = _ScriptedClient(
        [
            ([], 429, 0.0, {"Retry-After": "2"}),
            ([_frame("ok"), _frame(finish_reason="STOP")], 200, 0.0, None),
        ]
    )

    result = await _collect(
        _provider(
            client,
            total_timeout=30.0,
            retry_base_seconds=7.0,  # would be used if the header were ignored
            sleep=_sleep,
        )
    )

    assert result == ["ok"]
    # The server said 2 seconds. Guessing 7 when it has told us exactly
    # is strictly worse information.
    assert slept == [pytest.approx(2.0)]


async def test_a_long_retry_after_fails_fast_without_waiting():
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    client = _ScriptedClient(
        [
            ([], 429, 0.0, {"Retry-After": "90"}),
            ([_frame("unreachable")], 200, 0.0, None),
        ]
    )

    with pytest.raises(LLMRateLimitError):
        await _collect(_provider(client, total_timeout=300.0, sleep=_sleep))

    # Ninety seconds is not a retry, it is a hang. The user gets the
    # rate-limit sentence now instead of a spinner for a minute and a
    # half, and the request handler is freed.
    assert slept == []
    assert client.attempts == 1


async def test_the_retry_after_threshold_is_configurable():
    from app.core.config import settings

    assert settings.LLM_RETRY_AFTER_MAX_SECONDS > 0


async def test_a_retry_after_http_date_is_parsed():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    when = datetime.now(timezone.utc) + timedelta(seconds=2)
    client = _ScriptedClient(
        [
            ([], 429, 0.0, {"Retry-After": format_datetime(when)}),
            ([_frame("ok"), _frame(finish_reason="STOP")], 200, 0.0, None),
        ]
    )

    # RFC 9110 allows either a delay in seconds or an HTTP-date. Treating
    # a date as unparseable would silently fall back to the guess.
    assert await _collect(_provider(client, total_timeout=30.0, sleep=_sleep)) == ["ok"]
    assert slept and 0 < slept[0] <= 3


async def test_a_garbage_retry_after_falls_back_to_the_backoff():
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    client = _ScriptedClient(
        [
            ([], 429, 0.0, {"Retry-After": "soon-ish"}),
            ([_frame("ok"), _frame(finish_reason="STOP")], 200, 0.0, None),
        ]
    )

    await _collect(
        _provider(client, total_timeout=30.0, retry_base_seconds=0.5, sleep=_sleep)
    )

    assert slept == [pytest.approx(0.5)]


class _DropsMidStreamResponse(_ScriptedResponse):
    """Yields one real frame, then the connection dies."""

    async def aiter_lines(self):
        import httpx

        yield _frame("Receipts must be sub")
        raise httpx.ReadTimeout("connection dropped mid-answer")


class _DropsMidStreamClient(_ScriptedClient):
    def stream(self, method, url, *, json, headers, timeout=None):
        self.stream_calls.append({"url": url, "json": json, "headers": headers})
        self.attempts += 1
        if self.attempts == 1:
            return _DropsMidStreamResponse([], 200, 0.0, None)
        return _ScriptedResponse(
            [_frame("a whole second answer"), _frame(finish_reason="STOP")], 200
        )


async def test_a_retryable_failure_after_the_first_token_is_still_not_retried():
    """A transport drop mid-answer is retryable in principle.

    It is not retried here, because the first token is already on the
    client's wire. A retry would deliver the opening of one answer
    followed by the whole of another, spliced together with no seam the
    client could detect.
    """
    from app.services.llm import LLMTimeoutError

    client = _DropsMidStreamClient([])
    pieces = []

    with pytest.raises(LLMTimeoutError):
        async for piece in _provider(client, total_timeout=30.0).stream(
            system="s", user="u"
        ):
            pieces.append(piece)

    assert pieces == ["Receipts must be sub"]
    assert client.attempts == 1

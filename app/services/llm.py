"""LLM provider interface, streaming.

The answering model is called with NO tools. Not an empty allowlist -- the
parameter is absent entirely. That is defence #2 against indirect prompt
injection: retrieved text is attacker-controlled, so instead of trying to
win an argument with it about what the rules are, the model is given
nothing it could be talked into using. There is no tool to exfiltrate
with, no retrieval to re-run, no file to read. The worst a successful
injection can achieve is a wrong answer in the tenant's own session --
which the citation validator then strips of any provenance it invented.
"""
import asyncio
import json
import logging
import uuid
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import AsyncIterator

from app.core.config import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Base for every provider failure the caller is expected to handle.

    The point of the hierarchy below is that `generate_answer` can turn a
    failure into a SPECIFIC sentence. "Something went wrong" is true of
    all of them and useful for none: a rate limit resolves by waiting, a
    safety block never resolves by waiting, and an empty response usually
    resolves by rephrasing. Collapsing them tells the user to do the
    wrong thing two times out of three.
    """


class LLMRateLimitError(LLMError):
    """429, or a 503 that means the same thing on a free tier.

    Carries `retry_after` when the provider sent the header, because the
    server knowing exactly how long to wait beats us guessing an
    exponential backoff.
    """

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMTimeoutError(LLMError):
    """The request never reached a decision: timeout, DNS, reset."""


class LLMDeadlineExceededError(LLMTimeoutError):
    """The TOTAL request budget expired.

    A subclass of LLMTimeoutError so the user-facing message stays
    "took too long", but a distinct type because it must NEVER be
    retried: there is no budget left to retry inside of, so a retry
    would either blow the deadline again or silently extend it.
    """


class LLMSafetyBlockedError(LLMError):
    """The provider refused -- before generating, or part-way through."""


class LLMEmptyResponseError(LLMError):
    """The call succeeded and produced no text at all."""


class LLMUpstreamError(LLMError):
    """Any other provider-side failure."""


# User-facing text for each failure. Deliberately plain: they appear in
# the answer bubble, so they must read as sentences rather than as
# incident reports, and they must never name a model, a provider or a
# status code -- that is operator detail and belongs in the log.
SAFETY_BLOCKED_ANSWER = (
    "I can't answer that question. The request was blocked by the content "
    "safety filter."
)
EMPTY_RESPONSE_ANSWER = (
    "I wasn't able to produce an answer for that. Please try rephrasing "
    "the question."
)
RATE_LIMITED_ANSWER = (
    "The answering service is busy right now. Please try again in a moment."
)
TIMEOUT_ANSWER = (
    "The answering service took too long to respond. Please try again."
)
GENERIC_ERROR_ANSWER = "Something went wrong generating an answer. Please try again."


def safe_answer_for(exc: BaseException) -> str:
    """Map a provider failure onto the sentence the user should read."""
    if isinstance(exc, LLMSafetyBlockedError):
        return SAFETY_BLOCKED_ANSWER
    if isinstance(exc, LLMEmptyResponseError):
        return EMPTY_RESPONSE_ANSWER
    if isinstance(exc, LLMRateLimitError):
        return RATE_LIMITED_ANSWER
    if isinstance(exc, LLMTimeoutError):
        return TIMEOUT_ANSWER
    return GENERIC_ERROR_ANSWER


class LLMProvider(ABC):
    @property
    @abstractmethod
    def model(self) -> str: ...

    @abstractmethod
    def stream(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """Yield answer text incrementally.

        Streaming is not a nicety here: retrieval + rerank already cost
        several hundred milliseconds before the model starts, so without
        streaming the user stares at nothing for seconds.
        """

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Non-streaming call, for the query rewriter."""


class AnthropicLLM(LLMProvider):
    def __init__(self, *, api_key: str, model: str, client=None):
        self._model = model
        if client is not None:
            self._client = client
        else:
            # Lazy import so a deployment using the fake provider needs
            # neither the SDK nor a key at import time.
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=api_key)

    @property
    def model(self) -> str:
        return self._model

    async def stream(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
        # Accepted and not filled: this provider has never been run
        # against a real API (the SDK is not even installed), so
        # claiming token counts from it would be inventing them.
        usage_sink: dict | None = None,
    ) -> AsyncIterator[str]:
        # No `tools=` argument. See the module docstring.
        async with self._client.messages.stream(
            model=self._model,
            max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
            # Low rather than zero: grounded extraction wants determinism,
            # but exactly 0 makes near-identical questions produce brittle,
            # occasionally degenerate phrasing.
            temperature=(
                settings.LLM_TEMPERATURE if temperature is None else temperature
            ),
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            async for chunk in stream.text_stream:
                yield chunk

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
            temperature=(
                settings.LLM_TEMPERATURE if temperature is None else temperature
            ),
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )


# A finishReason in this set means the model stopped because it would not
# continue, not because it was done. The partial text is NOT an answer:
# half a refused sentence reads exactly like a complete one.
_REFUSAL_FINISH_REASONS = {
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "IMAGE_SAFETY",
}
# Statuses that mean "try later" rather than "this request is wrong".
_RATE_LIMIT_STATUS = {429, 503, 529}


def _parse_retry_after(value) -> float | None:
    """RFC 9110 Retry-After: either delay-seconds or an HTTP-date.

    Returns None for anything unrecognised, so a malformed header falls
    back to our own backoff rather than failing the request.
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(text)
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _classify_http(exc: BaseException) -> LLMError:
    """Map a transport/HTTP exception onto the domain hierarchy.

    Reads the status code where one exists rather than matching exception
    class names, because every client library names them differently.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        if status in _RATE_LIMIT_STATUS:
            headers = getattr(response, "headers", None) or {}
            retry_after = _parse_retry_after(
                headers.get("retry-after") or headers.get("Retry-After")
            )
            return LLMRateLimitError(
                f"provider rate limited ({status})", retry_after=retry_after
            )
        if status == 408:
            return LLMTimeoutError("provider timed out (408)")
        return LLMUpstreamError(f"provider error {status}")

    # No status: the request never reached a decision. Same "connect"
    # rather than "connection" trap as in embedding.classify_error --
    # httpx.ConnectError lowercases to "connecterror".
    name = type(exc).__name__.lower()
    if any(hint in name for hint in ("timeout", "connect", "network", "protocol")):
        return LLMTimeoutError(f"provider unreachable: {type(exc).__name__}")
    return LLMUpstreamError(f"provider call failed: {type(exc).__name__}")


def _visible_text(candidate: dict) -> str:
    """The parts of one candidate a user is meant to read.

    Gemini marks reasoning parts with `"thought": true`. It only sends
    them when `thinkingConfig.includeThoughts` is set, which this client
    never sets -- so today this filter removes nothing, and that is the
    reason to have it: `gemini-3.5-flash-lite` reports
    `"thinking": true`, rejects `thinkingBudget: 0` with a 400, and the
    day anyone turns includeThoughts on, an unfiltered concatenation
    splices the model's private reasoning into the cited answer and into
    the stored Message row. A filter that costs nothing now is cheaper
    than finding that in production.

    The test is `part.get("thought") is True`, not truthiness: an absent
    key and an explicit false both mean "visible", and a part is kept
    unless it says otherwise.
    """
    parts = (candidate.get("content") or {}).get("parts") or []
    return "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("thought") is not True
    )


class GeminiLLM(LLMProvider):
    """Google Gemini over the REST API.

    Streams via `:streamGenerateContent?alt=sse`. The `alt=sse` matters:
    without it Gemini returns one buffered JSON array and "streaming"
    delivers nothing until the answer is finished, which defeats the
    entire reason for streaming here.

    Like the embedding provider, this talks REST through httpx rather
    than the google-genai SDK -- one less dependency, and an exception
    surface we can actually classify.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float,
        total_timeout: float | None = None,
        max_retries: int | None = None,
        retry_base_seconds: float | None = None,
        client=None,
        sleep=None,
    ):
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        # Per-operation stall limit, handed to httpx. Bounds how long ONE
        # read may wait, not how long the call may take.
        self._timeout = timeout
        # Total deadline for the whole call. A separate guarantee: a model
        # emitting one token every two seconds forever never trips a
        # per-read limit, because every individual read is fast.
        self._total_timeout = (
            settings.LLM_TOTAL_TIMEOUT_SECONDS if total_timeout is None else total_timeout
        )
        self._max_retries = (
            settings.LLM_MAX_RETRIES if max_retries is None else max_retries
        )
        self._retry_base_seconds = (
            settings.LLM_RETRY_BASE_SECONDS
            if retry_base_seconds is None
            else retry_base_seconds
        )
        self._client = client
        self._sleep = sleep or asyncio.sleep

    def __repr__(self) -> str:
        # Explicit, so an exception logged with the provider in scope
        # cannot print the key.
        return f"GeminiLLM(model={self._model!r})"

    __str__ = __repr__

    @property
    def model(self) -> str:
        return self._model

    def _url(self, method: str, *, sse: bool = False) -> str:
        suffix = "?alt=sse" if sse else ""
        return f"{self._base_url}/models/{self._model}:{method}{suffix}"

    def _payload(self, *, system: str, user: str, max_tokens, temperature) -> dict:
        # No "tools" key, and no "toolConfig". Not an empty list -- absent.
        # Retrieved text is attacker-controlled, so rather than trying to
        # win an argument with it about what the rules are, the model is
        # given nothing it could be talked into using.
        return {
            # systemInstruction, not a prepended user turn: the grounding
            # rules travel on a different channel from the retrieved
            # context, so injected text inside a document cannot present
            # itself as an instruction.
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens or settings.LLM_MAX_TOKENS,
                "temperature": (
                    settings.LLM_TEMPERATURE if temperature is None else temperature
                ),
            },
        }

    @property
    def _headers(self) -> dict:
        # Header auth, never ?key= -- a query parameter lands in proxy
        # logs, access logs and any exception that records a URL.
        return {"x-goog-api-key": self._api_key}

    async def stream(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
        usage_sink: dict | None = None,
    ) -> AsyncIterator[str]:
        """Stream the answer, retrying only BEFORE the first token.

        `usage_sink`, when given, is filled with the provider's own
        token counts from the final SSE frame's `usageMetadata`. A
        CALLER-SUPPLIED dict rather than an attribute on self: this
        provider is a process-wide singleton behind `get_llm()`, and
        two concurrent answers writing their usage onto the same object
        would bill one tenant for the other's tokens. The dict is the
        caller's, so there is one per request by construction.

        Counts come from the provider, never from a local tokenizer.
        tiktoken is not Gemini's tokenizer, so a local count is an
        estimate, and an estimate recorded in a metering table is a
        number someone will later treat as measured.

        Retrying after a token has been yielded is not an option: those
        bytes are already on the client's wire and cannot be recalled, so
        a second attempt would deliver the opening of one answer followed
        by the whole of another. Once `produced_text` is set, every
        failure propagates.
        """
        # Correlates every log line this one call emits. Generated here
        # rather than threaded through the interface, so it identifies
        # the LLM request specifically.
        request_id = uuid.uuid4().hex[:8]
        attempt = 0

        # ONE budget for the whole request, fixed before the first
        # attempt. A deadline that restarts per attempt is not a
        # deadline: with one retry a "120s" limit permits 240s plus
        # backoff, and the caller who configured 120 has no way to find
        # that out.
        deadline = asyncio.get_running_loop().time() + self._total_timeout

        while True:
            produced_text = False
            try:
                # The sink is CLEARED before each attempt, not merged
                # into. A retry is a second billable call to the
                # provider, and leaving the first attempt's counts in
                # place would report the tokens of an answer that was
                # discarded -- plus, after the retry writes, the sum of
                # two attempts as though it were one.
                if usage_sink is not None:
                    usage_sink.clear()
                async for piece in self._stream_once(
                    system=system,
                    user=user,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    request_id=request_id,
                    deadline=deadline,
                    usage_sink=usage_sink,
                ):
                    produced_text = True
                    yield piece
            except LLMDeadlineExceededError:
                # Never retried. There is no budget left to retry inside
                # of, so a retry either blows the deadline again or
                # silently extends the one the caller asked for.
                raise
            except (LLMRateLimitError, LLMTimeoutError) as exc:
                # Retryable in principle -- but only while nothing has
                # been sent to the client yet, and only within budget.
                if produced_text or attempt >= self._max_retries:
                    raise

                delay, give_up = self._retry_delay(exc, attempt)
                if give_up:
                    # Retry-After longer than we are willing to hold a
                    # request handler open. Waiting is not a retry at
                    # that point, it is a hang with extra steps.
                    logger.warning(
                        "llm call %s rate limited with Retry-After=%ss; "
                        "failing fast rather than holding the request",
                        request_id,
                        getattr(exc, "retry_after", None),
                    )
                    raise

                remaining = deadline - asyncio.get_running_loop().time()
                if delay >= remaining:
                    # Sleeping would consume the rest of the budget and
                    # leave nothing for the attempt itself. Fail now
                    # rather than sleep and then fail.
                    raise LLMDeadlineExceededError(
                        f"total deadline of {self._total_timeout}s exceeded "
                        f"while backing off"
                    ) from exc

                # The CAUSE, not just the class. "(LLMTimeoutError)"
                # alone cannot distinguish a read that stalled for 30s
                # from a connection refused in 20ms -- they are the same
                # class here and have nothing else in common. The key
                # rides a header, never the URL, so an httpx message is
                # safe to log; a test pins that.
                logger.warning(
                    "llm call %s failed before first token (%s: %s); "
                    "retry %s/%s in %.2fs",
                    request_id,
                    type(exc).__name__,
                    exc,
                    attempt + 1,
                    self._max_retries,
                    delay,
                )
                if delay:
                    await self._sleep(delay)
                attempt += 1
                continue
            return

    def _retry_delay(self, exc: BaseException, attempt: int) -> tuple[float, bool]:
        """How long to wait before the next attempt, and whether to give up.

        A provider that sent Retry-After has told us the answer; guessing
        an exponential backoff instead is strictly worse information. But
        a long Retry-After is a different message: "not soon". Honouring
        90 seconds inside a request handler is a hang, so past
        LLM_RETRY_AFTER_MAX_SECONDS we surface the rate-limit message
        immediately and let the user decide whether to try again.
        """
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            if retry_after > settings.LLM_RETRY_AFTER_MAX_SECONDS:
                return 0.0, True
            return retry_after, False
        return self._retry_base_seconds * (2**attempt), False

    async def _stream_once(
        self,
        *,
        system: str,
        user: str,
        max_tokens,
        temperature,
        request_id: str,
        deadline: float,
        usage_sink: dict | None = None,
    ) -> AsyncIterator[str]:
        payload = self._payload(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )
        url = self._url("streamGenerateContent", sse=True)

        produced_text = False
        # Counted, then logged once at the end. One line per bad frame
        # would let a provider having a bad minute bury every other log
        # entry in the file.
        malformed = 0
        client, close = await self._acquire_client()
        try:
            # timeout_AT an absolute instant, not timeout(duration):
            # the deadline was fixed in stream() before the first
            # attempt, so every attempt and every backoff shares one
            # budget. Distinct from the per-read limit handed to httpx,
            # which bounds a single read rather than the call.
            async with asyncio.timeout_at(deadline):
                try:
                    # timeout on the REQUEST, not on the client: the
                    # client is shared with providers whose budgets
                    # differ (the reranker gets 3s), and a client-level
                    # value would silently override LLM_TIMEOUT_SECONDS
                    # with the pool's backstop.
                    stream_cm = client.stream(
                        "POST",
                        url,
                        json=payload,
                        headers=self._headers,
                        timeout=self._timeout,
                    )
                except Exception as exc:
                    raise _classify_http(exc) from exc

                async with stream_cm as response:
                    if response.status_code >= 400:
                        # The body must be drained before the status can
                        # be raised on a streaming response, or httpx
                        # complains it was never read.
                        await response.aread()
                        try:
                            response.raise_for_status()
                        except Exception as exc:
                            raise _classify_http(exc) from exc

                    async for line in response.aiter_lines():
                        pieces, bad = self._pieces_from_line(
                            line, usage_sink=usage_sink
                        )
                        malformed += bad
                        for piece in pieces:
                            produced_text = True
                            yield piece
        except TimeoutError as exc:
            # asyncio.timeout_at raises TimeoutError on expiry. A
            # distinct type from a per-read stall, so the retry loop can
            # refuse to retry it and the logs say which limit fired.
            raise LLMDeadlineExceededError(
                f"total deadline of {self._total_timeout}s exceeded"
            ) from exc
        except LLMError:
            raise
        except asyncio.CancelledError:
            # The client disconnected. Propagated so the HTTP request to
            # Gemini is actually aborted rather than left running.
            raise
        except Exception as exc:
            raise _classify_http(exc) from exc
        finally:
            if malformed:
                logger.warning(
                    "llm call %s: skipped %d unparseable SSE frame(s) from %s",
                    request_id,
                    malformed,
                    self._model,
                    extra={"request_id": request_id, "malformed_frames": malformed},
                )
            if close is not None:
                await close()

        if not produced_text:
            # A clean finish with zero tokens. Without this the caller
            # yields an empty answer and the user watches a blank bubble
            # that never resolves -- the "hung stream" failure mode.
            raise LLMEmptyResponseError(
                f"{self._model} returned no text (finishReason was not a refusal)"
            )

    def _pieces_from_line(
        self, line: str, *, usage_sink: dict | None = None
    ) -> tuple[list[str], int]:
        """Parse one SSE line into (text pieces, malformed-frame count).

        Raises LLMSafetyBlockedError if the frame says the model refused.

        The return shape is unchanged and the sink is a keyword with a
        default, so existing callers and tests of this method are
        unaffected -- a third tuple element would have broken every one
        of them for a value most of them do not want.
        """
        line = (line or "").strip()
        if not line or not line.startswith("data:"):
            return [], 0
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            return [], 0

        try:
            frame = json.loads(data)
        except ValueError:
            # One malformed frame must not lose the frames around it. A
            # slightly short answer beats an exception over a bad chunk.
            return [], 1

        self._raise_if_refused(frame)

        if usage_sink is not None:
            # Gemini sends usageMetadata on the LAST frame of a stream,
            # and sometimes on earlier ones with partial counts. The
            # later frame overwrites the earlier, so what survives is
            # the final total. Keys are copied one at a time rather than
            # by update() so that a partial frame carrying only
            # promptTokenCount cannot delete a candidatesTokenCount an
            # earlier frame reported.
            usage = frame.get("usageMetadata")
            if isinstance(usage, dict):
                for key in (
                    "promptTokenCount",
                    "candidatesTokenCount",
                    "totalTokenCount",
                    # Recorded if present: thinking tokens are BILLED
                    # and are not part of the answer, so a cost figure
                    # that omits them is wrong in the expensive
                    # direction. Measured absent on
                    # gemini-3.5-flash-lite today.
                    "thoughtsTokenCount",
                ):
                    if isinstance(usage.get(key), int):
                        usage_sink[key] = usage[key]

        pieces = []
        for candidate in frame.get("candidates") or []:
            text = _visible_text(candidate)
            if text:
                pieces.append(text)
        return pieces, 0

    def _raise_if_refused(self, frame: dict) -> None:
        # Two distinct refusal channels. promptFeedback.blockReason means
        # the INPUT was rejected and no candidate was ever produced;
        # finishReason means generation started and was cut off.
        block_reason = (frame.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            raise LLMSafetyBlockedError(f"prompt blocked: {block_reason}")

        for candidate in frame.get("candidates") or []:
            finish = candidate.get("finishReason")
            if finish in _REFUSAL_FINISH_REASONS:
                raise LLMSafetyBlockedError(f"generation stopped: {finish}")
            # MAX_TOKENS is deliberately NOT a refusal: the text produced
            # so far is real and readable, just incomplete.

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        payload = self._payload(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )
        url = self._url("generateContent")

        client, close = await self._acquire_client()
        try:
            response = await client.post(
                url, json=payload, headers=self._headers, timeout=self._timeout
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise _classify_http(exc) from exc
        finally:
            if close is not None:
                await close()

        self._raise_if_refused(body)

        text = ""
        for candidate in body.get("candidates") or []:
            text += _visible_text(candidate)

        if not text:
            raise LLMEmptyResponseError(f"{self._model} returned no text")
        return text

    async def _acquire_client(self):
        """Return (client, close_or_None).

        An injected client belongs to the caller and must not be closed
        here. Neither must the SHARED pooled one -- closing it would tear
        down the connection pool every other request is using, which is
        the opposite of pooling. Hence the second element is now always
        None, kept so the caller's `finally` needs no change and so an
        implementation that does own its client can still say so.
        """
        if self._client is not None:
            return self._client, None

        from app.core.http import get_async_client

        return get_async_client(), None


class FakeLLM(LLMProvider):
    """Deterministic provider for tests.

    Records every prompt it was given, which is how the injection tests
    assert on prompt STRUCTURE rather than on model behaviour -- the only
    thing a test can actually pin down, since no amount of testing proves
    a real model will obey a rule.
    """

    def __init__(
        self,
        *,
        answer: str = "Receipts must be submitted within thirty days [1].",
        chunk_size: int = 8,
        delay: float = 0.0,
        error: Exception | None = None,
        model: str = "fake-llm",
    ):
        self._answer = answer
        self._chunk_size = chunk_size
        self._delay = delay
        self._error = error
        self._model = model
        self.calls: list[dict] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def last_call(self) -> dict:
        return self.calls[-1]

    async def stream(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
        usage_sink: dict | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self._error is not None:
            raise self._error
        for index in range(0, len(self._answer), self._chunk_size):
            if self._delay:
                # Yields control, so a test can observe cancellation
                # mid-stream the way a real disconnect would.
                await asyncio.sleep(self._delay)
            yield self._answer[index : index + self._chunk_size]

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self._error is not None:
            raise self._error
        return self._answer


def _gemini(model: str) -> "GeminiLLM":
    if not settings.GEMINI_API_KEY:
        # Loud at startup rather than as a 500 on the first question.
        raise RuntimeError("LLM_PROVIDER=gemini requires GEMINI_API_KEY")
    return GeminiLLM(
        api_key=settings.GEMINI_API_KEY,
        model=model,
        base_url=settings.GEMINI_BASE_URL,
        timeout=settings.LLM_TIMEOUT_SECONDS,
    )


@lru_cache(maxsize=1)
def get_llm() -> LLMProvider:
    if settings.LLM_PROVIDER == "fake":
        return FakeLLM()
    if settings.LLM_PROVIDER == "gemini":
        return _gemini(settings.LLM_MODEL)
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
    return AnthropicLLM(api_key=settings.ANTHROPIC_API_KEY, model=settings.LLM_MODEL)


@lru_cache(maxsize=1)
def get_judge_llm() -> LLMProvider:
    """The model that GRADES answers, never the one that writes them.

    Separate from `get_llm()` on purpose, even when both resolve to the
    same model name today. A judge sharing the answerer's weights shares
    its blind spots: the claims it is least likely to flag as
    unsupported are exactly the ones it would itself have inferred, and
    those are the interesting failures. Pointing JUDGE_MODEL at a
    different model is therefore a real improvement in the measurement
    -- and keeping it behind its own factory makes that a one-line
    config change instead of a code change under time pressure.

    Lives in app/ rather than in eval/ because it is a provider, and
    every other provider in this system is constructed in one place with
    one set of timeout, pool and error-classification rules. An eval-only
    copy would be the one client that does not share the connection pool
    and does not map a 429 onto LLMRateLimitError.
    """
    if settings.LLM_PROVIDER == "fake":
        return FakeLLM()
    if settings.LLM_PROVIDER == "gemini":
        return _gemini(settings.JUDGE_MODEL)
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
    return AnthropicLLM(api_key=settings.ANTHROPIC_API_KEY, model=settings.JUDGE_MODEL)


@lru_cache(maxsize=1)
def get_rewrite_llm() -> LLMProvider:
    """A separate, cheaper model for query rewriting.

    Rewriting runs on every request including cache hits, so it must be
    cheap; and it never sees retrieved text, so it does not need the
    answering model's capability.
    """
    if settings.LLM_PROVIDER == "fake":
        return FakeLLM(answer="rewritten question")
    if settings.LLM_PROVIDER == "gemini":
        return _gemini(settings.REWRITE_MODEL)
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
    return AnthropicLLM(api_key=settings.ANTHROPIC_API_KEY, model=settings.REWRITE_MODEL)

"""Gemini streaming LLM provider tests.

Three things are being pinned down:

  1. The request we build -- including the ABSENCE of a tools field,
     which is the project's second line of defence against indirect
     prompt injection and therefore a thing a test must notice if it ever
     reappears.
  2. SSE parsing: Gemini streams `data: {...}` frames, and text lives at
     candidates[0].content.parts[*].text.
  3. Every way the call can end badly -- safety block before or during
     generation, an empty response, a rate limit, a timeout -- maps onto
     a named domain error. A provider failure that escapes as a raw
     exception becomes a 500 or, worse, a stream that never closes.
"""
import json

import pytest

from app.services.llm import (
    GeminiLLM,
    LLMEmptyResponseError,
    LLMRateLimitError,
    LLMSafetyBlockedError,
    LLMTimeoutError,
)

SECRET = "gemini-secret-key-value"


def _frame(text=None, finish_reason=None, prompt_block=None):
    """One SSE payload the way Gemini sends it."""
    body: dict = {}
    if text is not None or finish_reason is not None:
        candidate: dict = {}
        if text is not None:
            candidate["content"] = {"parts": [{"text": text}], "role": "model"}
        if finish_reason is not None:
            candidate["finishReason"] = finish_reason
        body["candidates"] = [candidate]
    if prompt_block is not None:
        body["promptFeedback"] = {"blockReason": prompt_block}
    return "data: " + json.dumps(body)


class _FakeStreamResponse:
    def __init__(self, lines, status=200):
        self._lines = lines
        self.status_code = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return b""

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "https://example.invalid")
            raise httpx.HTTPStatusError(
                "status " + str(self.status_code),
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeAsyncClient:
    def __init__(self, lines=None, status=200, payload=None, raises=None):
        self._lines = lines or []
        self._status = status
        self._payload = payload or {}
        self._raises = raises
        self.stream_calls: list[dict] = []
        self.post_calls: list[dict] = []

    def stream(self, method, url, *, json, headers):
        self.stream_calls.append(
            {"method": method, "url": url, "json": json, "headers": headers}
        )
        if self._raises is not None:
            raise self._raises
        return _FakeStreamResponse(self._lines, self._status)

    async def post(self, url, *, json, headers):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        if self._raises is not None:
            raise self._raises

        class _Response:
            status_code = self._status
            _body = self._payload

            def raise_for_status(inner):
                if inner.status_code >= 400:
                    import httpx

                    request = httpx.Request("POST", "https://example.invalid")
                    raise httpx.HTTPStatusError(
                        "status " + str(inner.status_code),
                        request=request,
                        response=httpx.Response(inner.status_code, request=request),
                    )

            def json(inner):
                return inner._body

        return _Response()


def _llm(client, *, model="gemini-3.5-flash-lite"):
    return GeminiLLM(
        api_key=SECRET,
        model=model,
        base_url="https://generativelanguage.googleapis.com/v1beta",
        timeout=10.0,
        client=client,
    )


async def _collect(provider, **kwargs):
    kwargs.setdefault("system", "sys")
    kwargs.setdefault("user", "usr")
    return [piece async for piece in provider.stream(**kwargs)]


# ------------------------------------------------------------- request ------


async def test_stream_targets_the_sse_endpoint_for_the_configured_model():
    client = _FakeAsyncClient(lines=[_frame("hi"), _frame(finish_reason="STOP")])

    await _collect(_llm(client, model="gemini-3.5-flash-lite"))

    call = client.stream_calls[0]
    assert call["method"] == "POST"
    # ?alt=sse is what turns a JSON array response into real SSE frames.
    # Without it Gemini buffers the whole array and "streaming" delivers
    # nothing until the answer is complete.
    assert call["url"] == (
        "https://generativelanguage.googleapis.com/v1beta"
        "/models/gemini-3.5-flash-lite:streamGenerateContent?alt=sse"
    )


async def test_stream_sends_the_system_prompt_as_systemInstruction():
    client = _FakeAsyncClient(lines=[_frame("hi"), _frame(finish_reason="STOP")])

    await _collect(_llm(client), system="GROUNDING RULES", user="the question")

    body = client.stream_calls[0]["json"]
    # systemInstruction, not a prepended user turn: the grounding rules
    # and the retrieved context must not share a channel, or injected
    # text in a document is indistinguishable from an instruction.
    assert body["systemInstruction"]["parts"][0]["text"] == "GROUNDING RULES"
    assert body["contents"][0]["role"] == "user"
    assert body["contents"][0]["parts"][0]["text"] == "the question"


async def test_stream_passes_generation_config_from_the_caller():
    client = _FakeAsyncClient(lines=[_frame("hi"), _frame(finish_reason="STOP")])

    await _collect(_llm(client), max_tokens=321, temperature=0.15)

    config = client.stream_calls[0]["json"]["generationConfig"]
    assert config["maxOutputTokens"] == 321
    assert config["temperature"] == pytest.approx(0.15)


async def test_the_request_carries_no_tools_field():
    client = _FakeAsyncClient(lines=[_frame("hi"), _frame(finish_reason="STOP")])

    await _collect(_llm(client))

    body = client.stream_calls[0]["json"]
    # Not an empty list -- absent. Retrieved text is attacker-controlled,
    # so the model is given nothing it could be argued into using: no
    # tool to exfiltrate with, no retrieval to re-run, no file to read.
    assert "tools" not in body
    assert "toolConfig" not in body


def test_no_tools_parameter_exists_on_the_interface():
    import inspect

    for method in ("stream", "complete"):
        assert "tools" not in inspect.signature(getattr(GeminiLLM, method)).parameters

    # Comments are stripped first, so the assertion is about the CODE and
    # not about the comment that explains why the code is the way it is.
    code = "\n".join(
        line.split("#")[0] for line in inspect.getsource(GeminiLLM).splitlines()
    )
    assert "tools" not in code
    assert "toolConfig" not in code


async def test_stream_authenticates_with_a_header_not_a_query_string():
    client = _FakeAsyncClient(lines=[_frame("hi"), _frame(finish_reason="STOP")])

    await _collect(_llm(client))

    call = client.stream_calls[0]
    assert call["headers"]["x-goog-api-key"] == SECRET
    assert SECRET not in call["url"]


# --------------------------------------------------------- sse parsing ------


async def test_stream_yields_text_pieces_in_order():
    client = _FakeAsyncClient(
        lines=[
            _frame("Receipts "),
            _frame("must be filed "),
            _frame("within thirty days [1]."),
            _frame(finish_reason="STOP"),
        ]
    )

    pieces = await _collect(_llm(client))

    assert pieces == ["Receipts ", "must be filed ", "within thirty days [1]."]


async def test_stream_ignores_blank_lines_and_the_done_sentinel():
    client = _FakeAsyncClient(
        lines=["", _frame("one"), "", "data: [DONE]", _frame(finish_reason="STOP"), ""]
    )

    assert await _collect(_llm(client)) == ["one"]


async def test_stream_survives_a_frame_that_is_not_json():
    client = _FakeAsyncClient(
        lines=[_frame("one"), "data: {not json", _frame("two"), _frame(finish_reason="STOP")]
    )

    # A malformed frame mid-stream must not lose the frames around it; a
    # partial answer beats an exception over one bad chunk.
    assert await _collect(_llm(client)) == ["one", "two"]


async def test_stream_joins_multiple_parts_in_one_frame():
    body = {
        "candidates": [
            {"content": {"parts": [{"text": "alpha "}, {"text": "beta"}], "role": "model"}}
        ]
    }
    client = _FakeAsyncClient(
        lines=["data: " + json.dumps(body), _frame(finish_reason="STOP")]
    )

    assert await _collect(_llm(client)) == ["alpha beta"]


async def test_max_tokens_is_not_an_error():
    client = _FakeAsyncClient(
        lines=[_frame("a truncated answer"), _frame(finish_reason="MAX_TOKENS")]
    )

    # Truncated, but real text the user can read. Raising here would
    # throw away an answer that is merely incomplete.
    assert await _collect(_llm(client)) == ["a truncated answer"]


# -------------------------------------------------------------- errors ------


async def test_a_safety_block_before_generation_raises():
    client = _FakeAsyncClient(lines=[_frame(prompt_block="SAFETY")])

    with pytest.raises(LLMSafetyBlockedError):
        await _collect(_llm(client))


async def test_a_safety_finish_reason_mid_stream_raises():
    client = _FakeAsyncClient(
        lines=[_frame("here is what I foun"), _frame(finish_reason="SAFETY")]
    )

    # The partial text must NOT be silently returned as the answer: the
    # model stopped because it would not finish the sentence, and half a
    # refused answer reads as a complete one.
    with pytest.raises(LLMSafetyBlockedError):
        await _collect(_llm(client))


async def test_a_recitation_finish_reason_raises():
    client = _FakeAsyncClient(lines=[_frame("verbatim"), _frame(finish_reason="RECITATION")])

    with pytest.raises(LLMSafetyBlockedError):
        await _collect(_llm(client))


async def test_a_stream_with_no_text_at_all_raises_empty_response():
    client = _FakeAsyncClient(lines=[_frame(finish_reason="STOP")])

    # Zero tokens and a clean finish. Without this the caller yields an
    # empty answer and the user sees a blank bubble that never resolves.
    with pytest.raises(LLMEmptyResponseError):
        await _collect(_llm(client))


async def test_a_completely_empty_stream_raises_empty_response():
    with pytest.raises(LLMEmptyResponseError):
        await _collect(_llm(_FakeAsyncClient(lines=[])))


async def test_429_raises_a_rate_limit_error():
    client = _FakeAsyncClient(lines=[], status=429)

    with pytest.raises(LLMRateLimitError):
        await _collect(_llm(client))


async def test_503_raises_a_rate_limit_error():
    # Gemini returns 503 UNAVAILABLE when the free tier is saturated;
    # from the caller's side it means the same thing as a 429.
    client = _FakeAsyncClient(lines=[], status=503)

    with pytest.raises(LLMRateLimitError):
        await _collect(_llm(client))


async def test_a_timeout_raises_a_timeout_error():
    import httpx

    client = _FakeAsyncClient(raises=httpx.ReadTimeout("too slow"))

    with pytest.raises(LLMTimeoutError):
        await _collect(_llm(client))


async def test_a_connect_error_raises_a_timeout_error():
    import httpx

    client = _FakeAsyncClient(raises=httpx.ConnectError("dns"))

    with pytest.raises(LLMTimeoutError):
        await _collect(_llm(client))


async def test_the_api_key_never_appears_in_an_error():
    import httpx

    client = _FakeAsyncClient(raises=httpx.ConnectError("dns"))
    provider = _llm(client)

    with pytest.raises(LLMTimeoutError) as excinfo:
        await _collect(provider)

    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(provider)
    assert SECRET not in str(provider)


# ------------------------------------------------------------ complete ------


async def test_complete_returns_joined_text_for_the_rewriter():
    payload = {
        "candidates": [
            {
                "content": {"parts": [{"text": "a standalone question?"}]},
                "finishReason": "STOP",
            }
        ]
    }
    client = _FakeAsyncClient(payload=payload)

    result = await _llm(client).complete(system="s", user="u", max_tokens=64)

    assert result == "a standalone question?"
    # Non-streaming endpoint: the rewriter needs the whole string before
    # it can do anything, so streaming it would only add latency.
    assert client.post_calls[0]["url"].endswith(":generateContent")
    assert client.post_calls[0]["json"]["generationConfig"]["maxOutputTokens"] == 64


async def test_complete_raises_on_a_safety_block():
    client = _FakeAsyncClient(payload={"promptFeedback": {"blockReason": "SAFETY"}})

    with pytest.raises(LLMSafetyBlockedError):
        await _llm(client).complete(system="s", user="u")


async def test_complete_raises_on_an_empty_response():
    client = _FakeAsyncClient(payload={"candidates": []})

    with pytest.raises(LLMEmptyResponseError):
        await _llm(client).complete(system="s", user="u")


# ------------------------------------------------------------ factories -----


def test_the_rewriter_uses_the_cheaper_configured_model(monkeypatch):
    from app.core.config import settings
    from app.services import llm as llm_module

    monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(settings, "LLM_MODEL", "big-answering-model")
    monkeypatch.setattr(settings, "REWRITE_MODEL", "small-cheap-model")
    llm_module.get_llm.cache_clear()
    llm_module.get_rewrite_llm.cache_clear()
    try:
        # The rewriter runs on EVERY request, cache hits included, and it
        # never sees retrieved text -- so it neither needs nor should pay
        # for the answering model.
        assert llm_module.get_llm().model == "big-answering-model"
        assert llm_module.get_rewrite_llm().model == "small-cheap-model"
    finally:
        llm_module.get_llm.cache_clear()
        llm_module.get_rewrite_llm.cache_clear()


def test_gemini_provider_requires_a_key(monkeypatch):
    from app.core.config import settings
    from app.services import llm as llm_module

    monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    llm_module.get_llm.cache_clear()
    try:
        # Loud at startup rather than as a 500 on the first question.
        with pytest.raises(RuntimeError):
            llm_module.get_llm()
    finally:
        llm_module.get_llm.cache_clear()

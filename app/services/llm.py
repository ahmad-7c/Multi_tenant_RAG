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
import logging
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import AsyncIterator

from app.core.config import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    pass


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


@lru_cache(maxsize=1)
def get_llm() -> LLMProvider:
    if settings.LLM_PROVIDER == "fake":
        return FakeLLM()
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
    return AnthropicLLM(api_key=settings.ANTHROPIC_API_KEY, model=settings.LLM_MODEL)


@lru_cache(maxsize=1)
def get_rewrite_llm() -> LLMProvider:
    """A separate, cheaper model for query rewriting.

    Rewriting runs on every request including cache hits, so it must be
    cheap; and it never sees retrieved text, so it does not need the
    answering model's capability.
    """
    if settings.LLM_PROVIDER == "fake":
        return FakeLLM(answer="rewritten question")
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
    return AnthropicLLM(api_key=settings.ANTHROPIC_API_KEY, model=settings.REWRITE_MODEL)

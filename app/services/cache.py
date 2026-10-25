"""The answer cache.

Key = sha256(org_id | normalized rewritten question | index_version)

Each of those three components is load-bearing, and leaving any one out
breaks something different:

  - **org_id**: without it, two tenants asking the same question share a
    key, and the second one is served the FIRST tenant's answer --
    including its citations, which name the first tenant's documents. That
    is a cross-tenant data leak that bypasses every org_id filter in the
    retrieval layer, because a cache hit never reaches the database at
    all. It is the single worst bug this system could have.
  - **the REWRITTEN question**: "what about contractors?" is a different
    question in every conversation. Keying on the raw text would serve one
    conversation's answer into an unrelated one, inside the same tenant.
  - **index_version**: without it, cached answers survive corpus changes.
    A question answered before a document was deleted keeps being answered
    from that document, citing a file the user can no longer open; a
    question answered before a correction was uploaded keeps returning the
    old, wrong answer. Since index_version is part of the key rather than
    a thing to invalidate, a bump makes every stale entry simply
    unreachable -- no scan, no delete, no race.

Only successful, cited answers are cached. A "not found" answer must not
be: the most likely reason for it is that the relevant document had not
finished ingesting yet, and caching that would keep answering "not found"
for the whole TTL after the content became searchable.
"""
import hashlib
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from functools import lru_cache

from app.core.config import settings
from app.services.query_rewrite import normalize_question

logger = logging.getLogger(__name__)

CACHE_PREFIX = "documind:answer:"


def answer_cache_key(
    *, org_id: uuid.UUID, question: str, index_version: int
) -> str:
    """Hashed, and org_id goes INSIDE the hash.

    Hashed because a raw question can be long and can contain anything;
    Redis keys are easier to reason about at fixed length. org_id is part
    of the hashed material rather than a key prefix so there is no way to
    construct a request that lands on another tenant's key -- a prefix can
    be confused by a question containing the delimiter, a hash cannot.
    """
    material = "\x00".join(
        [str(org_id), normalize_question(question), str(index_version)]
    )
    return CACHE_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class CachedAnswer:
    answer: str
    citations: list[dict]
    model: str
    cached_at: float


class AnswerCache(ABC):
    @abstractmethod
    async def get(self, key: str) -> CachedAnswer | None: ...

    @abstractmethod
    async def set(self, key: str, value: CachedAnswer, ttl: int) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class RedisAnswerCache(AnswerCache):
    def __init__(self, url: str, client=None):
        if client is not None:
            self._client = client
        else:
            # redis.asyncio, not the sync client: this runs inside the
            # request handler and a blocking round-trip would stall the
            # event loop for every other in-flight request.
            from redis import asyncio as aioredis

            self._client = aioredis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> CachedAnswer | None:
        try:
            raw = await self._client.get(key)
        except Exception:
            # A cache outage must never fail a request. Treated as a miss,
            # which costs latency and money but still answers.
            logger.warning("answer cache read failed; treating as a miss", exc_info=True)
            return None
        if not raw:
            return None
        try:
            return CachedAnswer(**json.loads(raw))
        except (ValueError, TypeError):
            # A payload written by an older code version. Dropping it is
            # correct: a half-understood cached answer is worse than a miss.
            logger.warning("discarding unreadable cache entry %s", key)
            return None

    async def set(self, key: str, value: CachedAnswer, ttl: int) -> None:
        try:
            await self._client.set(key, json.dumps(asdict(value)), ex=ttl)
        except Exception:
            # Same reasoning: failing to cache is not failing to answer.
            logger.warning("answer cache write failed", exc_info=True)

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass


class InMemoryAnswerCache(AnswerCache):
    """For tests and single-process development.

    Honours TTL so expiry is actually testable, and counts hits/misses so
    a test can assert a cache HIT rather than merely asserting the answer
    was right (which it would also be on a miss).
    """

    def __init__(self):
        self._store: dict[str, tuple[CachedAnswer, float]] = {}
        self.hits = 0
        self.misses = 0

    async def get(self, key: str) -> CachedAnswer | None:
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        value, expires_at = entry
        if expires_at <= time.monotonic():
            del self._store[key]
            self.misses += 1
            return None
        self.hits += 1
        return value

    async def set(self, key: str, value: CachedAnswer, ttl: int) -> None:
        self._store[key] = (value, time.monotonic() + ttl)

    async def close(self) -> None:
        self._store.clear()


def should_cache(*, answer: str, citations: list, gated: bool, errored: bool) -> bool:
    """Only successful, cited answers are cacheable.

    - `gated` (the confidence gate refused): the likeliest cause is that
      the relevant document had not finished ingesting. Caching it would
      keep answering "not found" for the whole TTL after the content became
      searchable.
    - `errored`: a provider blip is not an answer.
    - no citations: either the model refused or every marker was dropped as
      invalid. Both mean there is nothing grounded to serve again.
    """
    if gated or errored:
        return False
    if not citations:
        return False
    return bool(answer and answer.strip())


@lru_cache(maxsize=1)
def get_answer_cache() -> AnswerCache:
    if settings.ENV == "test" or not settings.REDIS_URL:
        return InMemoryAnswerCache()
    return RedisAnswerCache(settings.CACHE_REDIS_URL or settings.REDIS_URL)

"""Per-tenant rate limiting, stream slots and a daily budget guard.

## Why a token bucket and not a window

Three candidates, and the choice matters more than it looks:

**Fixed window** ("300 per minute, counter resets on the minute") is one
INCR and is wrong at the boundary. A client sending 300 at 11:59:59 and
300 at 12:00:00 sends 600 requests inside one second and never exceeds
the limit. For an endpoint whose cost is an LLM call, that burst is the
exact thing being prevented.

**Sliding window log** (timestamps in a sorted set, count what is still
inside the window) has no boundary bug, and stores one member per
request. At 300/min per org that is 300 members per org kept for a
minute, plus a ZREMRANGEBYSCORE on every call, to answer a question a
single float can answer.

**Token bucket** (used here) stores two numbers -- a token count and the
time it was last updated -- and refills by elapsed time on read. Burst
is a deliberate parameter (the capacity) rather than an artifact of
where the minute boundary falls, which is what a question-answering
product actually wants: a user firing three follow-ups in five seconds
is normal, a client firing three hundred is not.

## Why the check MUST be atomic

The read-modify-write is the whole limiter:

    tokens = GET key          # 2 left
    if tokens >= 1:           # both workers see 2
        SET key tokens - 1    # both write 1

Two API workers handling two simultaneous requests both read 2, both
allow, and both write 1 -- so two tokens were spent and one was
recorded. Under real concurrency the limit becomes advisory, and it
fails open at exactly the moment load is highest, which is when it was
supposed to work. Hence one Lua script: Redis runs it to completion
before any other command on that key.

Time comes from Redis's own `TIME` inside the script, not from the
caller. Several API processes mean several clocks; a caller-supplied
timestamp lets a process with a fast clock mint tokens that have not
been earned, and one with a slow clock be throttled by its neighbours.
(Effect-based replication, default since Redis 5, is what makes a
non-deterministic command legal in a script.)

## Why keys are per org, never global

One tenant must not be able to exhaust another tenant's share. A global
limiter turns a single noisy customer into an outage for everybody,
reported as "the product is down" rather than "that tenant is loud". The
org id comes from the verified JWT, never from the request -- a
caller-supplied org id would let anyone pick a fresh, empty bucket per
request.

Login and register are the exception: there is no org yet, and the
abuse they attract is credential stuffing from one source against many
accounts. Those are keyed by client IP.

## Redis unavailable

Deliberately asymmetric, and this is the pre-approved policy:

  * **auth and read endpoints fail OPEN.** Refusing logins because a
    cache is down converts a Redis incident into a total outage, and
    nothing downstream of them costs money per call.
  * **the answer endpoint fails CLOSED** -- onto a small per-process
    in-memory bucket, not onto a hard 503. With Redis down the budget
    guard and the cross-process limiter are both blind, so an unlimited
    answer endpoint is an unlimited bill. A per-process bucket is not a
    correct limiter (N processes means N buckets) but it bounds the
    worst case to N x limit instead of infinity.

Every fallback activation is logged at WARNING with the key, because
silent degradation into a weaker limiter is how a bill arrives.
"""
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------- scripts --

# Token bucket. KEYS[1] = bucket key.
# ARGV: 1 refill rate (tokens/second), 2 capacity, 3 cost, 4 key ttl.
# Returns {allowed, tokens_remaining, retry_after_seconds}.
#
# Returned as STRINGS because Lua -> Redis integer conversion truncates
# floats, and a retry_after of 1.7s truncated to 1 tells the client to
# come back before it will be allowed -- which produces a second 429 and
# a client that believes our Retry-After is wrong.
TOKEN_BUCKET_LUA = """
local key      = KEYS[1]
local rate     = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local cost     = tonumber(ARGV[3])
local ttl      = tonumber(ARGV[4])

local t   = redis.call('TIME')
local now = tonumber(t[1]) + (tonumber(t[2]) / 1000000)

local data   = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts     = tonumber(data[2])

if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
end

-- A clock that moved backwards must not mint tokens.
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end

tokens = math.min(capacity, tokens + (elapsed * rate))

local allowed = 0
local retry_after = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_after = (cost - tokens) / rate
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return {allowed, tostring(tokens), tostring(retry_after)}
"""

# Concurrent stream slots. KEYS[1] = slot set key.
# ARGV: 1 limit, 2 slot ttl seconds, 3 slot id.
# Returns {acquired, in_use}.
#
# A sorted set of slot ids scored by acquisition time, NOT an INCR/DECR
# counter. A counter leaks: if a worker is killed between INCR and DECR
# -- an OOM, a deploy, a SIGKILL mid-stream -- the count never comes
# down, and after `limit` such events that org can never open a stream
# again until someone notices and deletes a key. Expiring members make
# the leak self-healing: a slot older than the ttl is evicted on the
# next acquire by anyone.
STREAM_SLOT_LUA = """
local key   = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl   = tonumber(ARGV[2])
local slot  = ARGV[3]

local t   = redis.call('TIME')
local now = tonumber(t[1]) + (tonumber(t[2]) / 1000000)

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - ttl)
local in_use = redis.call('ZCARD', key)

if in_use >= limit then
  return {0, in_use}
end

redis.call('ZADD', key, now, slot)
-- Twice the slot ttl: the key must outlive its newest member, and a
-- key that expires while a stream is open would silently reset the
-- count to zero mid-stream.
redis.call('EXPIRE', key, math.ceil(ttl * 2))
return {1, in_use + 1}
"""


class RateLimitExceeded(Exception):
    """Raised by a limiter, turned into a 429 by the dependency."""

    def __init__(self, retry_after: float, scope: str):
        self.retry_after = max(1, int(retry_after + 0.999))  # ceil, min 1s
        self.scope = scope
        super().__init__(f"rate limit exceeded for {scope}")


class BudgetExceeded(Exception):
    """Raised when a daily provider budget is spent."""

    def __init__(self, scope: str):
        self.scope = scope
        super().__init__(f"daily budget exhausted for {scope}")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: float
    retry_after: float
    # True when the decision came from the in-memory fallback rather
    # than Redis. Surfaced so /health can show the degraded state
    # instead of reporting a limiter that is not really protecting
    # anything across processes.
    degraded: bool = False


# --------------------------------------------------------------- buckets --


class Limiter(ABC):
    @abstractmethod
    async def check(
        self, key: str, *, per_minute: int, burst: int, cost: float = 1.0
    ) -> Decision: ...

    @abstractmethod
    async def acquire_stream_slot(self, org_id: str, *, limit: int) -> Optional[str]: ...

    @abstractmethod
    async def release_stream_slot(self, org_id: str, slot: str) -> None: ...


class InMemoryLimiter(Limiter):
    """Per-process buckets. The fallback, and the test double.

    Not a correct distributed limiter and not pretending to be: with N
    API processes an org gets N buckets, so the effective limit is N x
    the configured one. It exists because the alternative when Redis is
    down is an unbounded answer endpoint, and N x 30/min is a bill you
    can survive while infinity is not.
    """

    def __init__(self):
        self._buckets: dict[str, tuple[float, float]] = {}
        self._streams: dict[str, dict[str, float]] = {}

    async def check(
        self, key: str, *, per_minute: int, burst: int, cost: float = 1.0
    ) -> Decision:
        rate = per_minute / 60.0
        capacity = float(burst)
        now = time.monotonic()
        tokens, ts = self._buckets.get(key, (capacity, now))
        tokens = min(capacity, tokens + max(0.0, now - ts) * rate)
        if tokens >= cost:
            self._buckets[key] = (tokens - cost, now)
            return Decision(True, tokens - cost, 0.0, degraded=True)
        self._buckets[key] = (tokens, now)
        retry_after = (cost - tokens) / rate if rate > 0 else 60.0
        return Decision(False, tokens, retry_after, degraded=True)

    async def acquire_stream_slot(self, org_id: str, *, limit: int) -> Optional[str]:
        ttl = settings.STREAM_SLOT_TTL_SECONDS
        now = time.monotonic()
        slots = self._streams.setdefault(org_id, {})
        for slot_id, started in list(slots.items()):
            if now - started > ttl:
                slots.pop(slot_id, None)
        if len(slots) >= limit:
            return None
        slot = uuid.uuid4().hex
        slots[slot] = now
        return slot

    async def release_stream_slot(self, org_id: str, slot: str) -> None:
        self._streams.get(org_id, {}).pop(slot, None)


class RedisLimiter(Limiter):
    """The real limiter. One Lua script per decision.

    `fallback` is consulted only when Redis itself fails, and only for
    scopes the caller marks as must-not-fail-open.
    """

    def __init__(self, url: Optional[str] = None, client=None):
        if client is not None:
            self._client = client
        else:
            from redis import asyncio as aioredis

            self._client = aioredis.from_url(
                url or settings.RATE_LIMIT_REDIS_URL or settings.REDIS_URL,
                decode_responses=True,
            )
        self._fallback = InMemoryLimiter()
        # Counted rather than logged per request: a Redis outage would
        # otherwise produce one WARNING per request for its duration.
        self.fallback_activations = 0

    def _note_fallback(self, key: str, exc: Exception) -> None:
        self.fallback_activations += 1
        if self.fallback_activations == 1 or self.fallback_activations % 100 == 0:
            logger.warning(
                "rate limiter fell back to the in-process bucket for %s "
                "(activation %d): %s. Limits are now PER PROCESS and the "
                "daily budget guard is blind.",
                key,
                self.fallback_activations,
                exc,
            )

    async def check(
        self, key: str, *, per_minute: int, burst: int, cost: float = 1.0
    ) -> Decision:
        rate = per_minute / 60.0
        # TTL: long enough that a bucket cannot be evicted while it is
        # still throttling someone. A bucket that expires early hands
        # back a full capacity, which is the limiter silently resetting.
        ttl = max(60, int(burst / rate) * 2 if rate > 0 else 60)
        try:
            allowed, remaining, retry_after = await self._client.eval(
                TOKEN_BUCKET_LUA, 1, key, rate, float(burst), float(cost), ttl
            )
        except Exception as exc:
            self._note_fallback(key, exc)
            raise RedisUnavailable(str(exc)) from exc
        return Decision(
            allowed=bool(int(allowed)),
            remaining=float(remaining),
            retry_after=float(retry_after),
        )

    async def acquire_stream_slot(self, org_id: str, *, limit: int) -> Optional[str]:
        slot = uuid.uuid4().hex
        try:
            acquired, _ = await self._client.eval(
                STREAM_SLOT_LUA,
                1,
                stream_key(org_id),
                limit,
                settings.STREAM_SLOT_TTL_SECONDS,
                slot,
            )
        except Exception as exc:
            self._note_fallback(stream_key(org_id), exc)
            raise RedisUnavailable(str(exc)) from exc
        return slot if int(acquired) else None

    async def release_stream_slot(self, org_id: str, slot: str) -> None:
        try:
            await self._client.zrem(stream_key(org_id), slot)
        except Exception:
            # A leaked slot expires on its own (see STREAM_SLOT_LUA), so
            # a failed release costs one slot for the ttl. Raising here
            # would turn a Redis blip during cleanup into an error on a
            # request that already succeeded.
            logger.warning("could not release stream slot %s", slot, exc_info=True)

    # ------------------------------------------------------- budget --

    async def consume_budget(self, org_id: str, *, units: int = 1) -> None:
        """Count provider work against the daily global and per-org caps.

        Counted AFTER the work, checked BEFORE it. Two separate keys
        rather than one: a global cap alone lets one tenant spend the
        whole day's quota by lunchtime, and a per-org cap alone sums to
        (orgs x share), which is unbounded as tenants are added.
        """
        day = _utc_day()
        try:
            pipe = self._client.pipeline()
            pipe.incrby(budget_key_global(day), units)
            pipe.expire(budget_key_global(day), settings.BUDGET_KEY_TTL_SECONDS)
            pipe.incrby(budget_key_org(org_id, day), units)
            pipe.expire(budget_key_org(org_id, day), settings.BUDGET_KEY_TTL_SECONDS)
            await pipe.execute()
        except Exception:
            # Losing a count under-reports usage, which is a metering
            # problem, not a reason to fail a request the user already
            # paid for in latency.
            logger.warning("budget counter write failed", exc_info=True)

    async def check_budget(self, org_id: str) -> None:
        day = _utc_day()
        try:
            values = await self._client.mget(
                budget_key_global(day), budget_key_org(org_id, day)
            )
        except Exception as exc:
            raise RedisUnavailable(str(exc)) from exc

        used_global = int(values[0] or 0)
        used_org = int(values[1] or 0)

        if settings.BUDGET_DAILY_GLOBAL_UNITS and (
            used_global >= settings.BUDGET_DAILY_GLOBAL_UNITS
        ):
            raise BudgetExceeded("global")
        if settings.BUDGET_DAILY_ORG_UNITS and (
            used_org >= settings.BUDGET_DAILY_ORG_UNITS
        ):
            raise BudgetExceeded("org")

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass


class RedisUnavailable(Exception):
    """Redis could not answer. The CALLER decides open or closed."""


# ------------------------------------------------------------------ keys --

PREFIX = "rl:"


def answer_key(org_id: str) -> str:
    return f"{PREFIX}answer:org:{org_id}"


def default_key(org_id: str) -> str:
    return f"{PREFIX}default:org:{org_id}"


def ip_key(scope: str, ip: str) -> str:
    return f"{PREFIX}{scope}:ip:{ip}"


def stream_key(org_id: str) -> str:
    return f"{PREFIX}streams:org:{org_id}"


def budget_key_global(day: str) -> str:
    return f"{PREFIX}budget:global:{day}"


def budget_key_org(org_id: str, day: str) -> str:
    return f"{PREFIX}budget:org:{org_id}:{day}"


def _utc_day() -> str:
    """The budget day, in UTC.

    UTC rather than a local or per-tenant day: a provider quota resets
    on the provider's clock, and a per-tenant midnight would make the
    global cap reset at a different time for every tenant counted into
    it.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%d")

"""The limiter: buckets, atomicity, per-org isolation, fail modes.

The hardest property to test is the one that matters most. A limiter
that is merely *approximately* right is a limiter that fails open under
concurrency -- which is precisely when it is needed -- so
`test_the_bucket_is_atomic_under_concurrency` fires many simultaneous
requests at one bucket through the real Lua script and asserts that
exactly `burst` were allowed. Against a non-atomic read-modify-write
that count comes out high, and it comes out high *sometimes*, which is
why the test uses enough concurrency to make the race reliable.

The Redis-backed tests skip when Redis is unreachable rather than fail.
Redis is local infrastructure like the test Postgres, not a provider:
the suite's offline guarantee is about never calling a paid API, and a
container on 6379 is not one. The in-memory tests below cover the same
logic with no dependency, so a skip loses the atomicity proof and
nothing else.
"""
import asyncio
import uuid

import pytest

from app.core.config import settings
from app.services.rate_limit import (
    BudgetExceeded,
    InMemoryLimiter,
    RedisLimiter,
    RedisUnavailable,
    answer_key,
    budget_key_global,
    budget_key_org,
    default_key,
    ip_key,
    stream_key,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture()
async def redis_limiter():
    """A RedisLimiter against the local Redis, with its keys cleaned up."""
    try:
        from redis import asyncio as aioredis
    except ImportError:  # pragma: no cover
        pytest.skip("redis package not installed")

    client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.close()
        pytest.skip(
            "Redis is not reachable on REDIS_URL. Start it with "
            "`docker compose up -d redis`. The in-memory tests in this "
            "file still cover the bucket logic; only the ATOMICITY proof "
            "is lost."
        )

    limiter = RedisLimiter(client=client)
    # A unique prefix per test, so two tests never share a bucket and a
    # leftover key from a previous run cannot decide today's result.
    limiter.test_prefix = f"test:{uuid.uuid4().hex[:8]}:"
    yield limiter

    keys = [k async for k in client.scan_iter(match=f"*{limiter.test_prefix}*")]
    if keys:
        await client.delete(*keys)
    await client.close()


def key_for(limiter, name: str) -> str:
    return f"rl:{getattr(limiter, 'test_prefix', '')}{name}"


# --------------------------------------------------------- bucket shape --


async def test_a_full_bucket_allows_exactly_its_burst():
    limiter = InMemoryLimiter()
    allowed = 0
    for _ in range(10):
        decision = await limiter.check("k", per_minute=60, burst=3)
        allowed += 1 if decision.allowed else 0
    # Three, not ten and not one: burst IS the capacity, and the whole
    # reason for a bucket rather than a fixed window is that the burst
    # is a chosen number rather than an artifact of clock alignment.
    assert allowed == 3


async def test_a_rejected_call_reports_a_retry_after_it_can_honour():
    limiter = InMemoryLimiter()
    for _ in range(3):
        await limiter.check("k", per_minute=60, burst=3)
    decision = await limiter.check("k", per_minute=60, burst=3)
    assert decision.allowed is False
    # 60/min = 1 token/sec, so one token is ~1s away. A Retry-After of
    # 0 would send the client straight back into a second 429.
    assert 0.5 < decision.retry_after <= 1.5


async def test_tokens_refill_over_time(monkeypatch):
    limiter = InMemoryLimiter()
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        "app.services.rate_limit.time.monotonic", lambda: clock["t"]
    )

    for _ in range(3):
        assert (await limiter.check("k", per_minute=60, burst=3)).allowed
    assert not (await limiter.check("k", per_minute=60, burst=3)).allowed

    clock["t"] += 2.0  # 60/min = 1 token/s, so two tokens are back
    assert (await limiter.check("k", per_minute=60, burst=3)).allowed
    assert (await limiter.check("k", per_minute=60, burst=3)).allowed
    assert not (await limiter.check("k", per_minute=60, burst=3)).allowed


async def test_refill_is_capped_at_the_burst(monkeypatch):
    limiter = InMemoryLimiter()
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        "app.services.rate_limit.time.monotonic", lambda: clock["t"]
    )
    await limiter.check("k", per_minute=60, burst=3)

    clock["t"] += 10_000.0  # an idle hour
    allowed = 0
    for _ in range(10):
        allowed += 1 if (await limiter.check("k", per_minute=60, burst=3)).allowed else 0
    # 3, not 10. Without the cap an idle tenant accumulates an unbounded
    # allowance and the first thing they do after lunch is unlimited.
    assert allowed == 3


async def test_a_backwards_clock_does_not_mint_tokens(monkeypatch):
    limiter = InMemoryLimiter()
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        "app.services.rate_limit.time.monotonic", lambda: clock["t"]
    )
    for _ in range(3):
        await limiter.check("k", per_minute=60, burst=3)

    clock["t"] -= 500.0  # NTP step, or a monotonic clock that is not
    assert not (await limiter.check("k", per_minute=60, burst=3)).allowed


# ------------------------------------------------------- key separation --


async def test_two_orgs_do_not_share_a_bucket():
    limiter = InMemoryLimiter()
    a, b = answer_key("org-a"), answer_key("org-b")
    for _ in range(3):
        await limiter.check(a, per_minute=60, burst=3)
    assert not (await limiter.check(a, per_minute=60, burst=3)).allowed
    # The whole point of a multi-tenant limiter: one loud tenant must
    # not be able to produce an outage for everybody else.
    assert (await limiter.check(b, per_minute=60, burst=3)).allowed


async def test_the_answer_bucket_is_separate_from_the_default_one():
    limiter = InMemoryLimiter()
    org = "org-a"
    for _ in range(3):
        await limiter.check(answer_key(org), per_minute=60, burst=3)
    assert not (await limiter.check(answer_key(org), per_minute=60, burst=3)).allowed
    # Exhausting the expensive endpoint must not stop the org reading
    # its own document list.
    assert (await limiter.check(default_key(org), per_minute=60, burst=3)).allowed


def test_the_key_builders_cannot_collide():
    # An org literally named "ip:1.2.3.4" must not land on the same key
    # as the IP bucket for 1.2.3.4. The scope prefixes keep them apart.
    assert answer_key("x") != default_key("x")
    assert ip_key("login", "x") != ip_key("register", "x")
    assert stream_key("x") not in {answer_key("x"), default_key("x")}
    assert budget_key_global("20261031") != budget_key_org("x", "20261031")


# ------------------------------------------------- Redis: the real thing --


async def test_the_redis_bucket_allows_exactly_its_burst(redis_limiter):
    key = key_for(redis_limiter, "burst")
    allowed = 0
    for _ in range(10):
        allowed += 1 if (
            await redis_limiter.check(key, per_minute=60, burst=4)
        ).allowed else 0
    assert allowed == 4


async def test_the_bucket_is_atomic_under_concurrency(redis_limiter):
    """The property the whole design rests on.

    A read-modify-write limiter:

        tokens = GET key          # both see 5
        if tokens >= 1: SET key 4 # both allow, both write 4

    allows more than the capacity whenever two requests interleave, and
    it does so under load -- so the limiter fails open at the exact
    moment it is needed. 40 simultaneous calls against a capacity of 5
    makes that race reliable rather than occasional.
    """
    key = key_for(redis_limiter, "atomic")
    results = await asyncio.gather(
        *[redis_limiter.check(key, per_minute=60, burst=5) for _ in range(40)]
    )
    allowed = sum(1 for r in results if r.allowed)
    # Exactly 5. Not "about 5": Redis runs the script to completion
    # before any other command touches the key, so there is no window.
    assert allowed == 5


async def test_redis_buckets_are_per_key(redis_limiter):
    a = key_for(redis_limiter, "org-a")
    b = key_for(redis_limiter, "org-b")
    for _ in range(3):
        await redis_limiter.check(a, per_minute=60, burst=3)
    assert not (await redis_limiter.check(a, per_minute=60, burst=3)).allowed
    assert (await redis_limiter.check(b, per_minute=60, burst=3)).allowed


async def test_a_redis_decision_is_not_marked_degraded(redis_limiter):
    decision = await redis_limiter.check(
        key_for(redis_limiter, "clean"), per_minute=60, burst=2
    )
    # `degraded` is what /health reports on. A real Redis decision
    # marked degraded would make the health endpoint cry wolf forever.
    assert decision.degraded is False


async def test_a_dead_redis_raises_RedisUnavailable_rather_than_allowing():
    class DeadClient:
        async def eval(self, *a, **k):
            raise ConnectionError("no route to host")

    limiter = RedisLimiter(client=DeadClient())
    # Not a silent True. The CALLER decides open or closed, per
    # endpoint; a limiter that decides for everyone would have to pick
    # the wrong answer for one of them.
    with pytest.raises(RedisUnavailable):
        await limiter.check("k", per_minute=60, burst=3)
    assert limiter.fallback_activations == 1


# -------------------------------------------------------- stream slots --


async def test_stream_slots_are_capped_and_released():
    limiter = InMemoryLimiter()
    org = "org-a"
    slots = [await limiter.acquire_stream_slot(org, limit=2) for _ in range(3)]
    assert slots[0] and slots[1]
    assert slots[2] is None

    await limiter.release_stream_slot(org, slots[0])
    assert await limiter.acquire_stream_slot(org, limit=2) is not None


async def test_stream_slots_are_per_org():
    limiter = InMemoryLimiter()
    assert await limiter.acquire_stream_slot("a", limit=1) is not None
    assert await limiter.acquire_stream_slot("a", limit=1) is None
    assert await limiter.acquire_stream_slot("b", limit=1) is not None


async def test_a_stale_slot_is_reclaimed(monkeypatch):
    """The reason slots are a timestamped set and not a counter.

    A worker killed mid-stream -- OOM, deploy, SIGKILL -- never runs its
    release. With a plain INCR/DECR counter that slot is gone forever
    and after `limit` such events the org can never stream again until
    someone deletes a Redis key by hand. Expiring members make the leak
    self-healing.
    """
    limiter = InMemoryLimiter()
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        "app.services.rate_limit.time.monotonic", lambda: clock["t"]
    )

    assert await limiter.acquire_stream_slot("a", limit=1) is not None
    assert await limiter.acquire_stream_slot("a", limit=1) is None

    clock["t"] += settings.STREAM_SLOT_TTL_SECONDS + 1
    assert await limiter.acquire_stream_slot("a", limit=1) is not None


async def test_releasing_an_unknown_slot_is_harmless():
    limiter = InMemoryLimiter()
    # Reachable when a release races a ttl eviction. It must not raise:
    # the release runs in a `finally` on a request that already
    # succeeded.
    await limiter.release_stream_slot("a", "no-such-slot")


async def test_redis_stream_slots_are_capped(redis_limiter):
    org = f"{getattr(redis_limiter, 'test_prefix', '')}org"
    first = await redis_limiter.acquire_stream_slot(org, limit=2)
    second = await redis_limiter.acquire_stream_slot(org, limit=2)
    third = await redis_limiter.acquire_stream_slot(org, limit=2)
    assert first and second
    assert third is None

    await redis_limiter.release_stream_slot(org, first)
    assert await redis_limiter.acquire_stream_slot(org, limit=2) is not None


async def test_redis_stream_slots_are_atomic(redis_limiter):
    org = f"{getattr(redis_limiter, 'test_prefix', '')}race"
    results = await asyncio.gather(
        *[redis_limiter.acquire_stream_slot(org, limit=3) for _ in range(20)]
    )
    assert sum(1 for r in results if r is not None) == 3


# ------------------------------------------------------------- budget --


@pytest.fixture()
def unique_budget_day(monkeypatch):
    """A fresh budget DAY per test.

    The per-org budget key carries an org id and is naturally isolated.
    The GLOBAL key is not -- it is one key for the whole deployment, by
    design, and two budget tests therefore increment the same counter.
    They passed in isolation and the global-cap test failed only in the
    full suite, which is the worst shape a flake can have: it blames
    whichever test ran first.

    Faking the day is the right isolation here rather than deleting
    keys, because it leaves the real key NAMES intact -- a test that
    rewrote them would stop noticing if the naming changed.
    """
    import uuid as _uuid

    day = f"test{_uuid.uuid4().hex[:10]}"
    monkeypatch.setattr("app.services.rate_limit._utc_day", lambda: day)
    return day


async def test_the_budget_guard_trips_on_the_global_cap(
    redis_limiter, monkeypatch, unique_budget_day
):
    org = f"{getattr(redis_limiter, 'test_prefix', '')}org"
    monkeypatch.setattr(settings, "BUDGET_DAILY_GLOBAL_UNITS", 3)
    monkeypatch.setattr(settings, "BUDGET_DAILY_ORG_UNITS", 0)

    for _ in range(3):
        await redis_limiter.check_budget(org)  # under the cap: silent
        await redis_limiter.consume_budget(org, units=1)

    with pytest.raises(BudgetExceeded) as caught:
        await redis_limiter.check_budget(org)
    assert caught.value.scope == "global"


async def test_the_per_org_cap_trips_before_the_global_one(
    redis_limiter, monkeypatch, unique_budget_day
):
    """Both caps exist because neither alone is enough.

    A global cap alone lets one tenant spend the whole day's quota by
    lunchtime. A per-org cap alone sums to (orgs x share), which grows
    without bound as tenants are added.
    """
    prefix = getattr(redis_limiter, "test_prefix", "")
    loud, quiet = f"{prefix}loud", f"{prefix}quiet"
    monkeypatch.setattr(settings, "BUDGET_DAILY_GLOBAL_UNITS", 100)
    monkeypatch.setattr(settings, "BUDGET_DAILY_ORG_UNITS", 2)

    for _ in range(2):
        await redis_limiter.consume_budget(loud, units=1)

    with pytest.raises(BudgetExceeded) as caught:
        await redis_limiter.check_budget(loud)
    assert caught.value.scope == "org"

    # The quiet tenant is unaffected, which is the point of the share.
    await redis_limiter.check_budget(quiet)


async def test_a_zero_cap_disables_the_guard(
    redis_limiter, monkeypatch, unique_budget_day
):
    org = f"{getattr(redis_limiter, 'test_prefix', '')}org"
    monkeypatch.setattr(settings, "BUDGET_DAILY_GLOBAL_UNITS", 0)
    monkeypatch.setattr(settings, "BUDGET_DAILY_ORG_UNITS", 0)
    for _ in range(50):
        await redis_limiter.consume_budget(org, units=1)
    # 0 means off, which is the shipped default: a budget invented
    # without measuring real traffic would refuse service for a number
    # nobody chose.
    await redis_limiter.check_budget(org)


async def test_a_budget_counter_write_failure_does_not_raise():
    class HalfDeadClient:
        def pipeline(self):
            raise ConnectionError("gone")

    # Losing a count under-reports spend. Raising would break a request
    # the user has already been served in full.
    await RedisLimiter(client=HalfDeadClient()).consume_budget("org", units=1)

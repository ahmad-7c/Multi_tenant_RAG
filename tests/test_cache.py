"""Answer-cache tests.

The first test in this file is the most important test in the project. A
cache hit never reaches the database, so it bypasses every org_id filter in
the retrieval layer -- which means the cache key is the ONLY thing standing
between two tenants.
"""
import asyncio
import uuid

import pytest

from app.core.config import settings
from app.services.cache import (
    CachedAnswer,
    InMemoryAnswerCache,
    answer_cache_key,
    should_cache,
)
from app.services.query_rewrite import normalize_question


# --------------------------------------------------------------- the key ----


def test_cache_never_serves_org_a_answer_to_org_b():
    """The single worst bug this system could have.

    Two tenants ask the identical question. A cache hit short-circuits
    before any SQL runs, so if the key matched, Org B would receive Org A's
    answer AND its citations -- which name Org A's documents.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    question = "What is the expense limit for meals?"

    key_a = answer_cache_key(org_id=org_a, question=question, index_version=0)
    key_b = answer_cache_key(org_id=org_b, question=question, index_version=0)

    assert key_a != key_b


async def test_cache_lookup_is_isolated_between_orgs_end_to_end():
    cache = InMemoryAnswerCache()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    question = "What is the expense limit?"

    await cache.set(
        answer_cache_key(org_id=org_a, question=question, index_version=0),
        CachedAnswer(answer="Org A secret: 60 USD", citations=[{}], model="m", cached_at=0),
        60,
    )

    assert (
        await cache.get(
            answer_cache_key(org_id=org_b, question=question, index_version=0)
        )
        is None
    )


def test_index_version_bump_invalidates_the_key():
    """Stale answers become unreachable rather than needing deletion.

    Without index_version, a question answered before a document was
    deleted keeps being answered from that document -- citing a file the
    user can no longer open.
    """
    org_id = uuid.uuid4()
    question = "What is the policy?"

    before = answer_cache_key(org_id=org_id, question=question, index_version=3)
    after = answer_cache_key(org_id=org_id, question=question, index_version=4)

    assert before != after


async def test_bumped_index_version_misses_the_old_entry():
    cache = InMemoryAnswerCache()
    org_id = uuid.uuid4()
    question = "What is the policy?"

    await cache.set(
        answer_cache_key(org_id=org_id, question=question, index_version=3),
        CachedAnswer(answer="old answer", citations=[{}], model="m", cached_at=0),
        60,
    )

    # One integer bump makes every stale entry for this org unreachable at
    # once -- no scan, no delete, no race.
    assert (
        await cache.get(
            answer_cache_key(org_id=org_id, question=question, index_version=4)
        )
        is None
    )
    assert (
        await cache.get(
            answer_cache_key(org_id=org_id, question=question, index_version=3)
        )
        is not None
    )


def test_different_questions_get_different_keys():
    org_id = uuid.uuid4()
    a = answer_cache_key(org_id=org_id, question="can I expense this", index_version=0)
    b = answer_cache_key(
        org_id=org_id, question="can I not expense this", index_version=0
    )
    # Normalization must never be aggressive enough to collide these.
    assert a != b


def test_key_is_stable_for_equivalent_phrasing():
    org_id = uuid.uuid4()
    a = answer_cache_key(org_id=org_id, question="What is the limit?", index_version=0)
    b = answer_cache_key(org_id=org_id, question="  what is the LIMIT  ", index_version=0)
    assert a == b


def test_org_id_is_inside_the_hash_not_a_prefix():
    org_id = uuid.uuid4()
    key = answer_cache_key(org_id=org_id, question="q", index_version=0)
    # A prefix could be confused by a question containing the delimiter; a
    # hash cannot.
    assert str(org_id) not in key


# -------------------------------------------------------- normalisation ----


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("What is the limit?", "what is the limit"),
        ("  spaced   out  ", "spaced out"),
        ("Trailing!!!", "trailing"),
        ("Multiple???", "multiple"),
        ("", ""),
    ],
)
def test_normalize_question(raw, expected):
    assert normalize_question(raw) == expected


def test_normalisation_does_not_strip_internal_negation():
    # Stemming or stopword removal would collide these. It would also
    # serve the exact opposite answer.
    assert normalize_question("can I expense this") != normalize_question(
        "can I not expense this"
    )


# --------------------------------------------------------- what to cache ----


def test_gated_answers_are_never_cached():
    """A "not found" must not be cached.

    The likeliest cause is that the relevant document had not finished
    ingesting. Caching it would keep answering "not found" for the whole
    TTL after the content became searchable.
    """
    assert (
        should_cache(answer="not found in your documents", citations=[], gated=True, errored=False)
        is False
    )


def test_errored_answers_are_never_cached():
    assert should_cache(answer="oops", citations=[{}], gated=False, errored=True) is False


def test_uncited_answers_are_never_cached():
    # Either the model refused or every marker was dropped as invalid.
    # Both mean there is nothing grounded to serve again.
    assert should_cache(answer="Some prose.", citations=[], gated=False, errored=False) is False


def test_empty_answers_are_never_cached():
    assert should_cache(answer="   ", citations=[{}], gated=False, errored=False) is False


def test_successful_cited_answers_are_cached():
    assert (
        should_cache(answer="Thirty days [1].", citations=[{}], gated=False, errored=False)
        is True
    )


# ------------------------------------------------------------- behaviour ----


async def test_in_memory_cache_round_trip():
    cache = InMemoryAnswerCache()
    value = CachedAnswer(answer="a", citations=[{"marker": 1}], model="m", cached_at=0)

    await cache.set("k", value, 60)

    assert await cache.get("k") == value
    assert cache.hits == 1


async def test_in_memory_cache_honours_ttl():
    cache = InMemoryAnswerCache()
    await cache.set("k", CachedAnswer(answer="a", citations=[], model="m", cached_at=0), 0)
    await asyncio.sleep(0.01)

    assert await cache.get("k") is None


async def test_cache_counts_hits_and_misses():
    cache = InMemoryAnswerCache()
    await cache.get("absent")
    await cache.set("present", CachedAnswer(answer="a", citations=[], model="m", cached_at=0), 60)
    await cache.get("present")

    # Counted so a test can assert a cache HIT, not merely that the answer
    # was right -- which it would also be on a miss.
    assert (cache.hits, cache.misses) == (1, 1)


async def test_redis_cache_read_failure_is_a_miss_not_an_error():
    from app.services.cache import RedisAnswerCache

    class _Broken:
        async def get(self, key):
            raise ConnectionError("redis down")

        async def set(self, key, value, ex=None):
            raise ConnectionError("redis down")

        async def close(self):
            pass

    cache = RedisAnswerCache("redis://unused", client=_Broken())

    # A cache outage must never fail a request: it costs latency and money
    # but the system still answers.
    assert await cache.get("k") is None
    await cache.set("k", CachedAnswer(answer="a", citations=[], model="m", cached_at=0), 60)


async def test_unreadable_cache_payload_is_discarded():
    from app.services.cache import RedisAnswerCache

    class _Garbage:
        async def get(self, key):
            return "not json at all"

        async def close(self):
            pass

    cache = RedisAnswerCache("redis://unused", client=_Garbage())

    # A payload written by an older code version: dropping it beats
    # half-understanding it.
    assert await cache.get("k") is None


def test_ttl_comes_from_settings():
    assert settings.CACHE_TTL_SECONDS > 0

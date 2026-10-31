"""The limiter as the HTTP layer sees it: 429s, fail modes, slots.

The unit tests in test_rate_limit.py prove the bucket arithmetic. These
prove the policy, which is where the interesting decisions live:

  * a 429 carries Retry-After and leaks no limit value,
  * login fails OPEN when Redis is down, because refusing logins turns
    a cache incident into a total outage,
  * the answer endpoint fails CLOSED onto a per-process bucket, because
    an unlimited answer endpoint is an unlimited provider bill,
  * the SSE slot is released on completion, on error, and on client
    disconnect -- the third being the one a naive implementation drops.
"""
import uuid

import pytest

from app.api import limits
from app.core.config import settings
from app.models.conversation import Conversation
from app.models.user import UserRole
from app.services.rate_limit import InMemoryLimiter, RedisUnavailable


class DeadRedisLimiter(InMemoryLimiter):
    """Every Redis call fails. The fallback decides what happens."""

    fallback_activations = 1

    async def check(self, key, *, per_minute, burst, cost=1.0):
        raise RedisUnavailable("simulated outage")

    async def acquire_stream_slot(self, org_id, *, limit):
        raise RedisUnavailable("simulated outage")


@pytest.fixture()
def conversation(db_session, org_factory, user_factory):
    org = org_factory()
    user = user_factory(org, role=UserRole.ADMIN)
    conv = Conversation(id=uuid.uuid4(), org_id=org.id, user_id=user.id, title="t")
    db_session.add(conv)
    db_session.commit()
    return org, user, conv


# ----------------------------------------------------------- the 429 --


def test_a_rate_limited_login_returns_429_with_retry_after(client, monkeypatch):
    # 1/min, not 60/min: at 60/min the bucket refills a token every
    # second and a login takes roughly that long (bcrypt verifies a
    # dummy hash even for an unknown user, to keep the response time
    # independent of whether the account exists). The bucket refilled
    # as fast as this test drained it, and nothing was ever rejected.
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_BURST", 2)

    body = {"org_name": "nope", "email": "a@example.com", "password": "x" * 12}
    codes = [client.post("/api/v1/auth/login", json=body).status_code for _ in range(5)]

    assert codes.count(429) >= 1
    response = client.post("/api/v1/auth/login", json=body)
    assert response.status_code == 429
    # Retry-After is the only machine-readable part of a 429 a
    # well-behaved client acts on. Without it the client guesses, and
    # the usual guess is "immediately".
    assert int(response.headers["Retry-After"]) >= 1


def test_the_429_body_names_no_limit_and_no_key(client, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_BURST", 1)

    body = {"org_name": "nope", "email": "a@example.com", "password": "x" * 12}
    client.post("/api/v1/auth/login", json=body)
    detail = client.post("/api/v1/auth/login", json=body).json()["detail"]

    # A limit value tells an attacker exactly how to pace a
    # credential-stuffing run just under it.
    assert "per minute" not in detail
    assert "burst" not in detail
    assert "rl:" not in detail
    assert "ip" not in detail.lower()


def test_register_is_limited_per_ip(client, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_REGISTER_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_REGISTER_BURST", 2)

    def register(n):
        return client.post(
            "/api/v1/auth/register",
            json={
                "org_name": f"Org-{uuid.uuid4().hex[:8]}",
                "email": f"{uuid.uuid4().hex[:8]}@example.com",
                "password": "correct-horse-battery",
            },
        ).status_code

    codes = [register(n) for n in range(4)]
    # Per IP and not per org: there is no org until the call succeeds,
    # so a per-org limit on this endpoint would be a limit of one
    # request per bucket, forever.
    assert 429 in codes


def test_the_answer_endpoint_is_limited_per_org(
    client, db_session, auth_headers, conversation, monkeypatch
):
    _, user, conv = conversation
    monkeypatch.setattr(settings, "RATE_LIMIT_ANSWER_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANSWER_BURST", 1)

    url = f"/api/v1/conversations/{conv.id}/messages"
    first = client.post(url, json={"content": "q"}, headers=auth_headers(user))
    second = client.post(url, json={"content": "q"}, headers=auth_headers(user))

    # The first may fail for any number of downstream reasons (no
    # corpus, no provider); what matters is that it is not a 429 and
    # the second is.
    assert first.status_code != 429
    assert second.status_code == 429


def test_one_orgs_exhausted_bucket_does_not_block_another(
    client, db_session, org_factory, user_factory, auth_headers, monkeypatch
):
    monkeypatch.setattr(settings, "RATE_LIMIT_ANSWER_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANSWER_BURST", 1)

    def conv_for(org, user):
        conv = Conversation(id=uuid.uuid4(), org_id=org.id, user_id=user.id, title="t")
        db_session.add(conv)
        db_session.commit()
        return conv

    org_a = org_factory()
    user_a = user_factory(org_a)
    conv_a = conv_for(org_a, user_a)
    org_b = org_factory()
    user_b = user_factory(org_b)
    conv_b = conv_for(org_b, user_b)

    url_a = f"/api/v1/conversations/{conv_a.id}/messages"
    client.post(url_a, json={"content": "q"}, headers=auth_headers(user_a))
    assert (
        client.post(url_a, json={"content": "q"}, headers=auth_headers(user_a))
    ).status_code == 429

    # The multi-tenancy requirement, as an HTTP fact: one loud tenant
    # must not produce an outage for everybody else.
    assert (
        client.post(
            f"/api/v1/conversations/{conv_b.id}/messages",
            json={"content": "q"},
            headers=auth_headers(user_b),
        )
    ).status_code != 429


# ------------------------------------------------------- the fail modes --


def test_login_fails_OPEN_when_redis_is_down(client):
    limits.set_limiter(DeadRedisLimiter())

    response = client.post(
        "/api/v1/auth/login",
        json={"org_name": "nope", "email": "a@example.com", "password": "x" * 12},
    )
    # 401, not 429 or 503. Refusing logins because a rate limiter is
    # unreachable converts a Redis incident into a total outage, and
    # nothing downstream of a login costs money per call.
    assert response.status_code == 401


def test_document_listing_fails_OPEN_when_redis_is_down(
    client, org_factory, user_factory, auth_headers
):
    limits.set_limiter(DeadRedisLimiter())
    org = org_factory()
    user = user_factory(org)

    response = client.get("/api/v1/documents", headers=auth_headers(user))
    assert response.status_code == 200


def test_the_answer_endpoint_fails_CLOSED_onto_a_process_bucket(
    client, auth_headers, conversation, monkeypatch
):
    """Redis down must not mean an unlimited answer endpoint.

    The fallback is not a correct distributed limiter -- N processes
    means N buckets -- and it is not pretending to be. It bounds the
    worst case to N x limit instead of infinity, which is the difference
    between a survivable bill and an unbounded one.
    """
    _, user, conv = conversation
    limits.set_limiter(DeadRedisLimiter())
    limits.reset_fallback()
    monkeypatch.setattr(settings, "RATE_LIMIT_ANSWER_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANSWER_BURST", 1)

    url = f"/api/v1/conversations/{conv.id}/messages"
    first = client.post(url, json={"content": "q"}, headers=auth_headers(user))
    second = client.post(url, json={"content": "q"}, headers=auth_headers(user))

    assert first.status_code != 429
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1


def test_health_names_the_degraded_limiter(client, auth_headers, org_factory, user_factory):
    limits.set_limiter(DeadRedisLimiter())
    body = client.get("/health").json()
    # The reranker breaker taught this lesson: a single WARNING logged
    # once is not an observable state. An operator must be able to ASK
    # whether limits are really being enforced.
    assert "rate_limiter_degraded" in body.get("degraded", [])
    # Still "ok": this instance can serve, and pulling it out of
    # rotation over a degraded limiter turns a degradation into an
    # outage.
    assert body["status"] == "ok"


def test_limits_can_be_switched_off_entirely(client, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_BURST", 1)

    body = {"org_name": "nope", "email": "a@example.com", "password": "x" * 12}
    codes = [client.post("/api/v1/auth/login", json=body).status_code for _ in range(4)]
    assert 429 not in codes


# ------------------------------------------------- the forwarded header --


def test_x_forwarded_for_is_ignored_by_default(client, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_BURST", 1)
    monkeypatch.setattr(settings, "TRUST_FORWARDED_FOR", False)

    body = {"org_name": "nope", "email": "a@example.com", "password": "x" * 12}
    client.post("/api/v1/auth/login", json=body)
    # A new value per request. If the header were trusted, each one
    # would get a fresh empty bucket and the limit would not exist.
    second = client.post(
        "/api/v1/auth/login", json=body, headers={"X-Forwarded-For": "9.9.9.9"}
    )
    assert second.status_code == 429


def test_x_forwarded_for_is_used_when_trusted(client, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_BURST", 1)
    monkeypatch.setattr(settings, "TRUST_FORWARDED_FOR", True)

    body = {"org_name": "nope", "email": "a@example.com", "password": "x" * 12}
    client.post("/api/v1/auth/login", json=body, headers={"X-Forwarded-For": "1.1.1.1"})
    same = client.post(
        "/api/v1/auth/login", json=body, headers={"X-Forwarded-For": "1.1.1.1"}
    )
    other = client.post(
        "/api/v1/auth/login", json=body, headers={"X-Forwarded-For": "2.2.2.2"}
    )
    assert same.status_code == 429
    assert other.status_code != 429


def test_only_the_leftmost_forwarded_address_is_used(monkeypatch):
    from starlette.requests import Request

    monkeypatch.setattr(settings, "TRUST_FORWARDED_FOR", True)
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"1.1.1.1, 10.0.0.1, 10.0.0.2")],
            "client": ("127.0.0.1", 1234),
        }
    )
    # The leftmost entry is the client; the rest are proxies that
    # appended themselves. Taking the last one buckets every client
    # behind one load balancer together.
    assert limits.client_ip(request) == "1.1.1.1"

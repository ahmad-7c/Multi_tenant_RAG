"""Rate-limit dependencies: a limiter decision turned into a response.

The limiter itself (`app/services/rate_limit.py`) decides allowed or
not. This module decides what happens when Redis cannot be reached,
which is a policy question and differs per endpoint:

  * `limit_answers` fails **CLOSED** onto a per-process bucket. With
    Redis down the cross-process limiter and the budget guard are both
    blind, and an unlimited answer endpoint is an unlimited provider
    bill.
  * `limit_default`, `limit_login`, `limit_register` fail **OPEN**.
    Refusing logins because a cache is down turns a Redis incident into
    a total outage, and none of those endpoints spends money per call.

## What a 429 says, and what it must not say

    HTTP/1.1 429 Too Many Requests
    Retry-After: 3
    {"detail": "Too many requests. Please retry in 3 seconds."}

`Retry-After` is a whole number of seconds and is rounded UP: the
remaining-token calculation gives 2.4s, and telling a client to return
in 2 produces a second 429 and a client that concludes our header is
wrong.

The body names no limit, no window, no key and no org. A limit value
tells an attacker exactly how to pace a credential-stuffing run, and
the org id in a body on an endpoint that has not authenticated anyone
is a disclosure. The operator detail goes to the log.

## Why the org id comes only from the token

`get_current_user` reads `org_id` from the verified JWT. A limiter keyed
on anything the caller can set -- a body field, a path parameter, a
header -- is not a limiter: the caller picks a fresh, empty bucket for
every request.
"""
import hmac
import ipaddress
import logging

from fastapi import Depends, HTTPException, Request, status

from app.api.deps import get_current_user
from app.core.config import settings
from app.models.user import User
from app.services.rate_limit import (
    BudgetExceeded,
    Decision,
    InMemoryLimiter,
    Limiter,
    RedisLimiter,
    RedisUnavailable,
    answer_key,
    default_key,
    ip_key,
)

logger = logging.getLogger(__name__)

# One limiter per process. Built lazily so importing the app does not
# open a Redis connection -- which would make every unit test that
# imports a route require a running Redis.
_LIMITER: Limiter | None = None
# The fallback is a SEPARATE object from the one inside RedisLimiter so
# that a test can install a fake Redis limiter and still exercise the
# fail-closed path.
_FALLBACK = InMemoryLimiter()

TOO_MANY = "Too many requests. Please retry in {seconds} seconds."
BUDGET_SPENT = (
    "This service has reached its daily capacity. Please try again tomorrow."
)


def get_limiter() -> Limiter:
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = RedisLimiter()
    return _LIMITER


def set_limiter(limiter: Limiter | None) -> None:
    """Install a limiter (tests, and the lifespan).

    A setter rather than an lru_cache: the tests need to swap the
    limiter between cases, and `cache_clear()` on a factory that opens a
    Redis connection would reopen one on the next call.
    """
    global _LIMITER
    _LIMITER = limiter


def reset_fallback() -> None:
    """Empty the per-process fallback buckets. Tests only."""
    global _FALLBACK
    _FALLBACK = InMemoryLimiter()


#: Longest plausible textual address, with room for an IPv6 scope id.
#: The value becomes part of a Redis key, so an unbounded one is a
#: memory problem wearing a header's clothing -- and this cap applies
#: even to a value that passed the secret check, because "authenticated"
#: is not the same as "well formed".
MAX_CLIENT_IP_CHARS = 64

#: Set by the BFF, never by Caddy. A distinct name from X-Forwarded-For
#: on purpose: Caddy overwrites that one with the connection's own peer
#: address, so a value sent in it would be discarded at the edge.
CLIENT_IP_HEADER = "x-client-ip"
CLIENT_IP_SECRET_HEADER = "x-client-ip-secret"


def _attested_client_ip(request: Request) -> str | None:
    """The browser's address, if the caller proved it may say so.

    Returns None whenever anything is missing, mismatched or malformed,
    so every failure path lands on the existing behaviour rather than on
    a half-trusted value.

    `hmac.compare_digest` rather than `==`: a plain comparison returns
    as soon as two bytes differ, so the time it takes reveals how much
    of the secret a guess got right. That is a remote oracle for
    recovering it one character at a time.
    """
    expected = settings.CLIENT_IP_SECRET
    # Unset means the feature is off. Checked FIRST, so an empty
    # configured secret can never be "matched" by an empty header.
    if not expected:
        return None

    presented = request.headers.get(CLIENT_IP_SECRET_HEADER)
    if not presented:
        return None
    if not hmac.compare_digest(presented, expected):
        return None

    candidate = (request.headers.get(CLIENT_IP_HEADER) or "").strip()
    if not candidate or len(candidate) > MAX_CLIENT_IP_CHARS:
        return None

    # Validated, not merely non-empty. A trusted sender can still be
    # buggy, and keying a limit on "undefined" or "" would put every
    # such request in one bucket -- which is the failure this whole
    # change exists to remove, re-created by accident.
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        logger.warning(
            "client-ip header passed the secret check but is not an address; "
            "falling back to the peer address"
        )
        return None
    return candidate


def client_ip(request: Request) -> str:
    """The address a per-IP limit is keyed on.

    Three sources, most trustworthy first:

    1. X-Client-IP, but ONLY when CLIENT_IP_SECRET is configured and the
       accompanying secret header matches it in constant time. This is
       the BFF telling us the browser's real address, which it alone
       can see.
    2. X-Forwarded-For, only when TRUST_FORWARDED_FOR is set, which is
       correct when Caddy is the sole ingress because Caddy overwrites
       that header with the connection's own peer address.
    3. The peer address.

    An unconditionally trusted header is strictly worse than no header:
    the caller sets a different value per request and receives a fresh,
    empty bucket each time, so the limit protects nothing while
    appearing to work. That is why 1 needs a secret and 2 needs a flag.
    The LEFTMOST X-Forwarded-For entry is the client; the rest are
    proxies.
    """
    attested = _attested_client_ip(request)
    if attested:
        return attested
    if settings.TRUST_FORWARDED_FOR:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    return request.client.host if request.client else "unknown"


def _reject(decision: Decision, scope: str, key: str) -> None:
    retry_after = max(1, int(decision.retry_after + 0.999))
    logger.info(
        "rate limited: scope=%s key=%s retry_after=%ss degraded=%s",
        scope,
        key,
        retry_after,
        decision.degraded,
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=TOO_MANY.format(seconds=retry_after),
        # Retry-After is the only machine-readable part of a 429 that a
        # well-behaved client acts on. Without it the client guesses,
        # and the usual guess is "immediately".
        headers={"Retry-After": str(retry_after)},
    )


async def _enforce(
    *, key: str, scope: str, per_minute: int, burst: int, fail_closed: bool
) -> None:
    if not settings.RATE_LIMIT_ENABLED:
        return

    limiter = get_limiter()
    try:
        decision = await limiter.check(key, per_minute=per_minute, burst=burst)
    except RedisUnavailable:
        if not fail_closed:
            # Fail open. Logged by the limiter, counted there, and
            # deliberately not an error for the caller: a login must not
            # depend on a rate limiter being reachable.
            return
        decision = await _FALLBACK.check(key, per_minute=per_minute, burst=burst)

    if not decision.allowed:
        _reject(decision, scope, key)


# ------------------------------------------------------------ the four --


async def limit_answers(
    request: Request, current_user: User = Depends(get_current_user)
) -> User:
    """The expensive endpoint. Per org, fails closed, checks the budget.

    Returns the user so a route can depend on this INSTEAD of
    `get_current_user` rather than in addition to it -- two dependencies
    resolving the same token would decode it twice and, more
    importantly, would let someone remove the limit while leaving auth
    in place and not notice.
    """
    org_id = str(current_user.org_id)
    await _enforce(
        key=answer_key(org_id),
        scope="answer",
        per_minute=settings.RATE_LIMIT_ANSWER_PER_MINUTE,
        burst=settings.RATE_LIMIT_ANSWER_BURST,
        fail_closed=True,
    )

    # The budget guard is checked AFTER the limiter: a client that is
    # already over its rate should get the 429 that tells it to slow
    # down, not a 503 that tells it to come back tomorrow.
    limiter = get_limiter()
    check_budget = getattr(limiter, "check_budget", None)
    if check_budget is not None and (
        settings.BUDGET_DAILY_GLOBAL_UNITS or settings.BUDGET_DAILY_ORG_UNITS
    ):
        try:
            await check_budget(org_id)
        except BudgetExceeded as exc:
            logger.warning("daily budget exhausted (%s) for org %s", exc.scope, org_id)
            _record_rejection(current_user)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=BUDGET_SPENT,
            ) from exc
        except RedisUnavailable:
            # The guard cannot be evaluated. Not a reason to refuse --
            # the rate limiter above has already failed closed onto a
            # per-process bucket, so the worst case is bounded.
            logger.warning("budget guard could not be evaluated; allowing")

    return current_user


def _record_rejection(user: User) -> None:
    """Meter a budget rejection. One INSERT, never raises.

    Only the BUDGET rejection is recorded, not the 429s, and the
    asymmetry is deliberate.

    A rate-limit rejection is unbounded by definition: it is what a
    client sends when it is sending too much, and an attacker's rejected
    requests would each buy a database write. Metering them turns the
    limiter into a DoS amplifier -- the cheapest possible response (a
    counter check and a 429) becomes a transaction.

    A budget rejection is different in kind. It happens at most at the
    answer endpoint's own rate (30/min/org, enforced immediately above
    this line), and it is the single most important thing an operator
    needs to see in this table: "we stopped answering at 14:20 because
    the day's quota was gone" is invisible if only successful work is
    recorded.
    """
    from app.db.session import SessionLocal
    from app.models.usage_event import UsageStatus
    from app.services import usage_service

    session = SessionLocal()
    try:
        usage_service.record(
            session,
            org_id=user.org_id,
            user_id=user.id,
            endpoint=usage_service.ENDPOINT_ANSWER,
            status=UsageStatus.REJECTED,
            # No latency: nothing was done. Recording a few
            # milliseconds of rejection would pollute the latency
            # average with the fastest responses the system produces.
            latency_ms=None,
        )
    finally:
        session.close()


async def limit_default(
    request: Request, current_user: User = Depends(get_current_user)
) -> User:
    await _enforce(
        key=default_key(str(current_user.org_id)),
        scope="default",
        per_minute=settings.RATE_LIMIT_DEFAULT_PER_MINUTE,
        burst=settings.RATE_LIMIT_DEFAULT_BURST,
        fail_closed=False,
    )
    return current_user


async def limit_login(request: Request) -> None:
    await _enforce(
        key=ip_key("login", client_ip(request)),
        scope="login",
        per_minute=settings.RATE_LIMIT_LOGIN_PER_MINUTE,
        burst=settings.RATE_LIMIT_LOGIN_BURST,
        fail_closed=False,
    )


async def limit_register(request: Request) -> None:
    await _enforce(
        key=ip_key("register", client_ip(request)),
        scope="register",
        per_minute=settings.RATE_LIMIT_REGISTER_PER_MINUTE,
        burst=settings.RATE_LIMIT_REGISTER_BURST,
        fail_closed=False,
    )

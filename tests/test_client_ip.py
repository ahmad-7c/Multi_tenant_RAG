"""The attested client-IP header, and every way it must NOT be trusted.

Behind the Next.js BFF every request arrives from one Vercel egress
address, so the per-IP limits on /auth/login and /auth/register are
per-DEPLOYMENT: credential stuffing is indistinguishable from normal
traffic, and a handful of real users signing in at once rate-limit each
other. The BFF therefore forwards the browser's real address and proves
it may do so with a shared secret.

Every test here is about the FAILURE direction. A header trusted when it
should not be is worse than no header at all: the caller picks a new
value per request, gets a fresh empty bucket every time, and the limit
protects nothing while still looking enforced.
"""
import ipaddress

import pytest

from app.api.limits import (
    CLIENT_IP_HEADER,
    CLIENT_IP_SECRET_HEADER,
    MAX_CLIENT_IP_CHARS,
    client_ip,
)

SECRET = "f" * 64  # 64 hex chars, the shape openssl rand -hex 32 gives.
BROWSER_IP = "203.0.113.7"  # TEST-NET-3, never routable.
PEER_IP = "198.51.100.9"  # TEST-NET-2: stands in for Vercel's egress.


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Enough of a Request for client_ip(): headers and a peer address."""

    def __init__(self, headers: dict[str, str], peer: str | None = PEER_IP) -> None:
        # Starlette lowercases header lookups; a plain dict with lowercase
        # keys is faithful for this function and keeps the test readable.
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.client = _FakeClient(peer) if peer else None


@pytest.fixture
def no_forwarded(monkeypatch):
    """TRUST_FORWARDED_FOR off, so only the attested path can win.

    Without this the X-Forwarded-For branch could satisfy an assertion
    that was meant to be about X-Client-IP, and the test would pass for
    the wrong reason.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "TRUST_FORWARDED_FOR", False)
    return settings


def test_the_header_is_used_when_the_secret_matches(no_forwarded, monkeypatch):
    """The happy path, so the rest of this file means something."""
    monkeypatch.setattr(no_forwarded, "CLIENT_IP_SECRET", SECRET)
    request = _FakeRequest(
        {CLIENT_IP_HEADER: BROWSER_IP, CLIENT_IP_SECRET_HEADER: SECRET}
    )
    assert client_ip(request) == BROWSER_IP


def test_a_wrong_secret_is_ignored(no_forwarded, monkeypatch):
    """The whole point. A near-miss must buy nothing at all."""
    monkeypatch.setattr(no_forwarded, "CLIENT_IP_SECRET", SECRET)
    request = _FakeRequest(
        {
            CLIENT_IP_HEADER: BROWSER_IP,
            # One character different, same length -- the case a
            # length-only check would wave through.
            CLIENT_IP_SECRET_HEADER: "f" * 63 + "e",
        }
    )
    assert client_ip(request) == PEER_IP


def test_no_secret_configured_ignores_the_header(no_forwarded, monkeypatch):
    """Unset is OFF, not "trust anything".

    This is the configuration every deployment starts in, and the one a
    forgotten Vercel variable leaves it in. If an unset secret meant
    "accept whatever arrives", the feature would be a vulnerability
    until someone remembered to configure it.
    """
    monkeypatch.setattr(no_forwarded, "CLIENT_IP_SECRET", "")
    request = _FakeRequest(
        {CLIENT_IP_HEADER: BROWSER_IP, CLIENT_IP_SECRET_HEADER: SECRET}
    )
    assert client_ip(request) == PEER_IP


def test_an_empty_secret_cannot_be_matched_by_an_empty_header(
    no_forwarded, monkeypatch
):
    """`"" == ""` must not be a match.

    compare_digest would happily agree that two empty strings are equal,
    so the unset check has to come FIRST. Pinned because the ordering is
    invisible in a diff: moving the `if not expected` below the header
    read turns an unconfigured deployment into one that trusts any
    caller who sends an empty secret.
    """
    monkeypatch.setattr(no_forwarded, "CLIENT_IP_SECRET", "")
    request = _FakeRequest(
        {CLIENT_IP_HEADER: BROWSER_IP, CLIENT_IP_SECRET_HEADER: ""}
    )
    assert client_ip(request) == PEER_IP


def test_a_spoofed_header_with_no_secret_header_is_ignored(
    no_forwarded, monkeypatch
):
    """A direct hit on the backend, bypassing the BFF entirely.

    The API is only reachable through Caddy, but "only reachable through
    Caddy" is a deployment fact, not a property of this function. Someone
    who can reach the port must not be able to choose a bucket just by
    naming one.
    """
    monkeypatch.setattr(no_forwarded, "CLIENT_IP_SECRET", SECRET)
    request = _FakeRequest({CLIENT_IP_HEADER: "1.2.3.4"})
    assert client_ip(request) == PEER_IP


def test_a_malformed_address_falls_back(no_forwarded, monkeypatch):
    """Authenticated is not the same as well formed.

    A trusted sender can still be buggy. Keying on "undefined" or
    "null" would put every such request in ONE bucket -- which is
    exactly the per-deployment failure this change exists to remove,
    re-created by accident and much harder to notice.
    """
    monkeypatch.setattr(no_forwarded, "CLIENT_IP_SECRET", SECRET)
    for junk in ("undefined", "null", "not-an-ip", "", "   ", "1.2.3", "999.1.1.1"):
        request = _FakeRequest(
            {CLIENT_IP_HEADER: junk, CLIENT_IP_SECRET_HEADER: SECRET}
        )
        assert client_ip(request) == PEER_IP, f"{junk!r} was accepted"


def test_an_overlong_value_is_rejected(no_forwarded, monkeypatch):
    """An overlong header does not become a Redis key.

    STATED LIMIT: this test cannot tell WHICH layer rejected it. Any
    string long enough to matter is also not a valid address, so
    removing the length cap leaves ipaddress.ip_address() to refuse it
    and this assertion still passes -- confirmed by mutation. The cap is
    pinned structurally in
    test_the_two_unobservable_defences_are_present_in_the_source.

    Kept anyway, because the OUTCOME is the thing that matters to a
    caller and it should be asserted somewhere.
    """
    monkeypatch.setattr(no_forwarded, "CLIENT_IP_SECRET", SECRET)
    request = _FakeRequest(
        {
            CLIENT_IP_HEADER: "2001:db8:" + "0:" * 400 + "1",
            CLIENT_IP_SECRET_HEADER: SECRET,
        }
    )
    assert len(request.headers[CLIENT_IP_HEADER]) > MAX_CLIENT_IP_CHARS
    assert client_ip(request) == PEER_IP


def test_the_two_unobservable_defences_are_present_in_the_source():
    """Two properties no functional test can see. Read, not exercised.

    1. CONSTANT-TIME COMPARISON. `==` and `hmac.compare_digest` return
       the same booleans, so swapping them is invisible to every test
       above -- confirmed by mutation: the whole file stayed green. What
       differs is that `==` returns as soon as two bytes differ, so the
       time it takes reveals how much of the secret a guess got right.
       That is a remote oracle for recovering it character by character,
       and the only way to pin it is to read the source.

    2. THE LENGTH CAP. Redundant with the ip_address() parse for every
       input anyone can construct, because a string long enough to be a
       problem is never a valid address. It exists so an unbounded value
       is discarded BEFORE being parsed rather than after, and removing
       it also leaves the suite green.

    Both are one-line changes a tidy-up could make in good faith. Same
    reasoning as the doubly-nulled citation score in B1: where two
    defences are redundant, each needs its own pin or the layer that
    goes is the one nobody notices.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "app" / "api" / "limits.py"
    # Comments stripped: both words appear in the prose explaining them,
    # so a naive search would find the explanation and not the code.
    code = "\n".join(
        line
        for line in source.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )

    assert "hmac.compare_digest(presented, expected)" in code, (
        "the secret comparison is no longer constant-time"
    )
    assert "len(candidate) > MAX_CLIENT_IP_CHARS" in code, (
        "the length cap on the client-IP header is gone"
    )


def test_ipv6_is_accepted(no_forwarded, monkeypatch):
    """IPv6 is a real address, not malformed.

    An over-strict validator would quietly send every IPv6 user into the
    shared peer bucket, which is the bug this change removes -- for a
    subset of users, where nobody would look.
    """
    monkeypatch.setattr(no_forwarded, "CLIENT_IP_SECRET", SECRET)
    v6 = "2001:db8::1"
    assert ipaddress.ip_address(v6)  # the premise, stated
    request = _FakeRequest({CLIENT_IP_HEADER: v6, CLIENT_IP_SECRET_HEADER: SECRET})
    assert client_ip(request) == v6


def test_the_attested_header_wins_over_forwarded_for(monkeypatch):
    """Precedence, and it matters which way round.

    Caddy overwrites X-Forwarded-For with its own peer address -- which
    behind the BFF is Vercel. If XFF won, configuring the secret would
    change nothing at all and the whole feature would be inert while
    appearing to be on.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "TRUST_FORWARDED_FOR", True)
    monkeypatch.setattr(settings, "CLIENT_IP_SECRET", SECRET)
    request = _FakeRequest(
        {
            CLIENT_IP_HEADER: BROWSER_IP,
            CLIENT_IP_SECRET_HEADER: SECRET,
            "x-forwarded-for": "10.0.0.1",
        }
    )
    assert client_ip(request) == BROWSER_IP


def test_forwarded_for_still_works_when_no_secret_is_configured(monkeypatch):
    """The Caddy-direct deployment is unchanged.

    This change must be invisible to anyone not using the BFF. With no
    secret set, behaviour is exactly what it was before.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "TRUST_FORWARDED_FOR", True)
    monkeypatch.setattr(settings, "CLIENT_IP_SECRET", "")
    request = _FakeRequest({"x-forwarded-for": "10.0.0.1, 192.0.2.1"})
    assert client_ip(request) == "10.0.0.1"


# --------------------------------------------------------------------
# The startup gate
# --------------------------------------------------------------------


def _problems(**overrides):
    from app.core.config import Settings
    from app.core.startup_checks import collect_production_problems

    base = dict(
        ENV="production",
        JWT_SECRET_KEY="x" * 64,
        DATABASE_URL="postgresql://dm:a-generated-password@db:5432/documind",
        RERANKER_PROVIDER="hosted",
        RERANKER_API_KEY="k" * 40,
        GEMINI_API_KEY="g" * 40,
        BUDGET_DAILY_GLOBAL_UNITS=1000,
        RATE_LIMIT_ENABLED=True,
        CELERY_TASK_ALWAYS_EAGER=False,
        CORS_ALLOW_ORIGINS="",
    )
    base.update(overrides)
    return collect_production_problems(Settings(**base))


def test_an_unset_client_ip_secret_is_accepted():
    """Unset is a valid production configuration, not an oversight.

    The limiter keeps using the peer address, which is correct for a
    deployment with no BFF in front. Refusing to boot over it would
    force every self-hosted install to configure a secret it has no use
    for.
    """
    assert not any("CLIENT_IP_SECRET" in p for p in _problems(CLIENT_IP_SECRET=""))


def test_a_short_client_ip_secret_refuses_to_boot():
    """A guessable secret is worse than none.

    It lets any caller nominate its own rate-limit bucket, which removes
    the per-IP limit while leaving it looking enforced -- and unlike an
    unset secret, nothing about the running system reveals it.
    """
    problems = _problems(CLIENT_IP_SECRET="tooshort")
    assert any("CLIENT_IP_SECRET" in p and "32" in p for p in problems)


def test_a_placeholder_client_ip_secret_refuses_to_boot():
    """The copy-paste case, caught by the same list as the signing key."""
    problems = _problems(CLIENT_IP_SECRET="change-me-" + "x" * 30)
    assert any("CLIENT_IP_SECRET" in p and "placeholder" in p for p in problems)


def test_the_secret_value_is_never_echoed():
    """A failed boot must not put the secret in the log.

    Same rule as JWT_SECRET_KEY: the message names the variable, never
    its contents. A deploy that fails loudly should not also disclose.
    """
    secret = "deadbeef" * 2  # 16 chars: short enough to be refused
    for problem in _problems(CLIENT_IP_SECRET=secret):
        assert secret not in problem

"""D-3, proven by BEHAVIOUR: a forged X-Forwarded-For is not used.

NOT part of the offline suite. Every other test in `tests/` runs with
no network by construction; this one needs the prod stack up behind
Caddy, so it skips unless EDGE_BASE_URL is set:

    docker compose -f docker-compose.prod.yml --env-file .env.production up -d
    EDGE_BASE_URL=https://localhost pytest tests/test_edge_live.py

WHY THIS AND NOT A FILE GREP. `test_prod_compose.py` already pins that
`header_up X-Forwarded-For {remote_host}` is in the Caddyfile. That
cannot tell a working directive from a typo, a placeholder Caddy does
not recognise, or a directive silently overridden by a global option.
The Caddyfile comment in this repo claimed for the whole of D1 that a
forged-header test existed. It did not, and in its absence
`trusted_proxies static private_ranges` sat in the same file
contradicting the claim. This is that test.

HOW IT OBSERVES THE PROPERTY. There is no endpoint that reflects the
client IP, and adding one would mean shipping a debug surface to
production to make a test easier. Instead it uses the mechanism that
actually consumes the value: the per-IP login limiter. `client_ip` in
app/api/limits.py reads the LEFTMOST X-Forwarded-For entry when
TRUST_FORWARDED_FOR is set, so:

  - header TRUSTED  -> every request carries a different forged IP,
                       gets its own fresh bucket, and no 429 ever comes
  - header IGNORED  -> all requests share the real peer's bucket and
                       the limit trips

So "no 429 after N attempts" is exactly the spoof succeeding. The
assertion is on the limiter firing, which is the consequence that
matters rather than a header echoed back.
"""
import os
import uuid

import pytest

httpx = pytest.importorskip("httpx")

BASE_URL = os.environ.get("EDGE_BASE_URL")

pytestmark = pytest.mark.skipif(
    not BASE_URL, reason="set EDGE_BASE_URL to run against a live edge"
)

LOGIN = "/api/v1/auth/login"

#: Comfortably above the login burst (5) so a correctly-shared bucket
#: must trip, while staying small enough that the test is quick.
ATTEMPTS = 12


def _client():
    # verify=False: Caddy serves `localhost` with a certificate from its
    # own locally-trusted CA, which this process has not been taught to
    # trust. The test is about a header, not about TLS trust -- and
    # teaching it the CA would mean reading Caddy's data volume.
    return httpx.Client(base_url=BASE_URL, verify=False, timeout=20.0)


def _bad_credentials():
    # A login that cannot succeed, so the test creates nothing and
    # authenticates as nobody. The limiter counts attempts either way.
    return {
        "org_name": f"no-such-org-{uuid.uuid4()}",
        "email": "nobody@example.com",
        "password": "not-a-real-password",
    }


def test_a_forged_forwarded_for_does_not_buy_a_fresh_rate_limit_bucket():
    """The spoof, attempted. Each request claims a different client IP."""
    statuses = []
    with _client() as client:
        for i in range(ATTEMPTS):
            response = client.post(
                LOGIN,
                json=_bad_credentials(),
                # A different forged origin every time. If Caddy passed
                # this through, each one would be a brand new bucket.
                headers={"X-Forwarded-For": f"203.0.113.{i + 1}"},
            )
            statuses.append(response.status_code)
            if response.status_code == 429:
                break

    assert 429 in statuses, (
        "no rate limit after %d attempts with a rotating forged "
        "X-Forwarded-For: the edge is passing the header through and the "
        "per-IP limiter is defeated" % ATTEMPTS
    )


def test_the_control_also_trips_without_any_forged_header():
    """Proves the test above measures the HEADER, not just a low limit.

    Without this, a limiter that refused everything for an unrelated
    reason would make the spoof test pass while proving nothing.
    """
    statuses = []
    with _client() as client:
        for _ in range(ATTEMPTS):
            response = client.post(LOGIN, json=_bad_credentials())
            statuses.append(response.status_code)
            if response.status_code == 429:
                break

    assert 429 in statuses, "the per-IP login limiter did not trip at all"


def test_the_rate_limited_response_carries_retry_after():
    """A 429 the client cannot act on is a dead end.

    Checked at the edge specifically: Caddy must not strip the header on
    its way out, which no backend test can observe.
    """
    with _client() as client:
        for _ in range(ATTEMPTS):
            response = client.post(LOGIN, json=_bad_credentials())
            if response.status_code == 429:
                assert "retry-after" in {k.lower() for k in response.headers}
                return
    pytest.fail("never reached a 429, so Retry-After was never observable")

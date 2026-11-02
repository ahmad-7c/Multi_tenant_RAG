"""CORS, which is OFF unless an operator turns it on.

The architecture decision is a same-origin BFF: the browser talks to
Next.js and Next.js server code talks to FastAPI, so in the normal
deployment there is no cross-origin request to permit and no CORS
header is wanted. That default is kept.

What this adds is a deliberate, named escape hatch for talking to the
API DIRECTLY -- a browser-based test client, Swagger on another port, a
separate frontend during development. It is driven entirely by
`CORS_ALLOW_ORIGINS`, defaults to empty, and never echoes an origin it
was not given.

The failure this is written to prevent is the common one: a wildcard
plus credentials, which would let any site on the internet make
authenticated requests with a user's cookies.

NB these tests build a THROWAWAY FastAPI app and run the real
`configure_cors` on it. The obvious alternative -- reloading app.main
so it re-reads the setting -- replaces the module-level `app` with a
new object while the rest of the suite still holds the old one, and
turned one CORS test into two unrelated failures in test_reranker_cohere
and test_stream_slots. Found by running the full suite, not this file.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as main
from app.core.config import Settings, settings


def _client(monkeypatch, origins: str) -> TestClient:
    """A bare app with only CORS and one route, configured as `origins`."""
    monkeypatch.setattr(settings, "CORS_ALLOW_ORIGINS", origins)
    throwaway = FastAPI()
    main.configure_cors(throwaway)

    @throwaway.get("/health")
    def _health():
        return {"status": "ok"}

    # A route that accepts POST, so a preflight has something to allow.
    @throwaway.post("/api/v1/conversations")
    def _conversations():
        return {"ok": True}

    return TestClient(throwaway)


# ----------------------------------------------------------- parsing ----


def test_cors_is_empty_by_default():
    """The shipped default adds no middleware at all.

    Asserted on a fresh Settings rather than the live one, because the
    live instance has read `.env` and a developer's local override must
    not be able to make this test pass.
    """
    assert Settings.model_fields["CORS_ALLOW_ORIGINS"].default == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", []),
        ("   ", []),
        ("http://localhost:3010", ["http://localhost:3010"]),
        # Whitespace and trailing commas are what a hand-edited .env
        # actually contains.
        (
            " http://localhost:3010 , https://app.example.com ,",
            ["http://localhost:3010", "https://app.example.com"],
        ),
        # A trailing slash is not part of an Origin and would never
        # match one, so it is stripped rather than silently failing.
        ("http://localhost:3010/", ["http://localhost:3010"]),
    ],
)
def test_origin_parsing(raw, expected, monkeypatch):
    monkeypatch.setattr(settings, "CORS_ALLOW_ORIGINS", raw)
    assert settings.cors_allow_origins() == expected


def test_a_wildcard_origin_is_refused(monkeypatch):
    """`*` with credentials is the mistake this guard exists for.

    Browsers reject the combination, so the practical result of
    configuring it is that every cross-origin call fails with an opaque
    CORS error -- hours of debugging for something that was never going
    to work. Refusing at startup turns that into one sentence.
    """
    monkeypatch.setattr(settings, "CORS_ALLOW_ORIGINS", "*")

    with pytest.raises(ValueError, match="wildcard"):
        settings.cors_allow_origins()


def test_a_wildcard_mixed_with_real_origins_is_also_refused(monkeypatch):
    monkeypatch.setattr(
        settings, "CORS_ALLOW_ORIGINS", "http://localhost:3010, *"
    )
    with pytest.raises(ValueError, match="wildcard"):
        settings.cors_allow_origins()


# ---------------------------------------------------------- behaviour ----


def test_no_cors_headers_when_unset(monkeypatch):
    """The BFF deployment: nothing is advertised to any origin."""
    client = _client(monkeypatch, "")

    response = client.get("/health", headers={"Origin": "http://localhost:3010"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_configured_origin_is_echoed_not_wildcarded(monkeypatch):
    client = _client(monkeypatch, "http://localhost:3010")

    response = client.get("/health", headers={"Origin": "http://localhost:3010"})

    assert response.status_code == 200
    # The exact origin, never "*" -- "*" is incompatible with
    # credentialed requests and would be useless here.
    assert response.headers["access-control-allow-origin"] == "http://localhost:3010"
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_an_unlisted_origin_gets_no_cors_headers(monkeypatch):
    """The allowlist is an allowlist."""
    client = _client(monkeypatch, "http://localhost:3010")

    response = client.get("/health", headers={"Origin": "https://evil.example"})

    # The request itself still succeeds -- CORS is enforced by the
    # BROWSER, not the server. What matters is that no header tells the
    # browser the response may be read.
    assert "access-control-allow-origin" not in response.headers


def test_preflight_is_answered_for_a_listed_origin(monkeypatch):
    client = _client(monkeypatch, "http://localhost:3010")

    response = client.options(
        "/api/v1/conversations",
        headers={
            "Origin": "http://localhost:3010",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3010"
    assert "POST" in response.headers["access-control-allow-methods"]


def test_retry_after_is_readable_cross_origin(monkeypatch):
    """A 429 carries Retry-After, and a cross-origin client cannot read
    a response header unless it is explicitly exposed.

    Without this the rate-limit countdown in a directly-connected test
    client silently reads undefined -- the header is present on the
    wire and invisible to JavaScript.
    """
    client = _client(monkeypatch, "http://localhost:3010")

    response = client.get("/health", headers={"Origin": "http://localhost:3010"})

    exposed = response.headers.get("access-control-expose-headers", "")
    assert "Retry-After" in exposed


def test_the_real_app_wires_cors_through_configure_cors():
    """The throwaway-app tests above prove the FUNCTION behaves.

    This proves `app.main` actually calls it, which is the half a
    helper-only test would miss: configure_cors could be perfect and
    never invoked. Read-only -- it inspects the live middleware stack
    rather than rebuilding the app, so it cannot pollute the suite.
    """
    from fastapi.middleware.cors import CORSMiddleware

    installed = [m.cls for m in main.app.user_middleware]
    expected = bool(settings.cors_allow_origins())

    assert (CORSMiddleware in installed) is expected, (
        "app.main's CORS middleware does not match CORS_ALLOW_ORIGINS; "
        "configure_cors is probably not being called"
    )

"""Refuse to start a production process that is configured unsafely.

Every other guard in this system DEGRADES. The embedding-model guard
keeps uploads and auth working and fails only the query routes; the
reranker breaker falls back to fused order; the rate limiter falls
back to a per-process bucket. That is right for those, because a
degraded answer is better than an outage.

These checks are different in kind, and they EXIT. A process signing
tokens with the documented placeholder secret has no safe degraded
mode: anyone who has read the repository can mint an admin token for
any organization. Serving at all is the failure.

They run only when ENV is a production value, so development keeps its
convenient defaults. Nothing here makes a network call -- key PRESENCE
is checked, never key validity. A startup probe that called a provider
would make boot depend on someone else's uptime and would bill for the
privilege.
"""
import re

from app.core.config import Settings

#: Values that have appeared in the repository, in docs, or in common
#: copy-paste sources. A secret that matches is public by definition.
PLACEHOLDER_SECRETS = frozenset(
    {
        "change-me",
        "change-me-generate-with-openssl-rand-hex-32",
        "changeme",
        "secret",
        "supersecret",
        "test",
        "dev",
        "development",
        "please-change-me",
        "your-secret-key",
        "your-secret-key-here",
    }
)

#: 32 bytes of entropy is the floor for HS256. Shorter keys are
#: brute-forcible offline, and an attacker holding one signed token can
#: grind it without touching the service.
MIN_JWT_SECRET_CHARS = 32

PRODUCTION_ENVS = frozenset({"production", "prod"})

#: Hosts that mean "this machine". An origin on one of them is a
#: development value, and granting it credentialed access in
#: production lets any process on a user's box call the API as them.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

#: Credentials that must not appear in a production DATABASE_URL. These
#: are the values docker-compose.yml ships for local development.
DEV_DB_MARKERS = ("documind:documind", "postgres:postgres", "user:password")


class UnsafeProductionConfig(RuntimeError):
    """Raised to stop the process. Carries every problem at once.

    All failures are collected rather than raised one at a time: an
    operator fixing a misconfigured deploy should see the whole list
    in one restart, not discover a second problem only after fixing
    the first.
    """


def is_production(settings: Settings) -> bool:
    return settings.ENV.strip().lower() in PRODUCTION_ENVS


def _absent(value: str | None) -> bool:
    """Absent means None, empty, OR whitespace.

    `GEMINI_API_KEY=` with nothing after it is the commonest way a key
    goes missing -- a commented example uncommented, or a paste that
    dropped. pydantic reads that as "" , not None, so a `is None`
    check would pass it and the first question of the day would be the
    thing that discovered it. A mutation swapping truthiness for
    `is None` survived the suite until this existed.
    """
    return not (value or "").strip()


def _looks_placeholder(value: str) -> bool:
    normalised = re.sub(r"[^a-z0-9-]", "", value.strip().lower())
    if normalised in PLACEHOLDER_SECRETS:
        return True
    # "change-me-anything-you-like" is still a placeholder.
    return normalised.startswith("change-me") or normalised.startswith("changeme")


def _origin_host(origin: str) -> str:
    """The hostname of an Origin, without scheme or port.

    Brackets are stripped before the port is split off, because
    `https://[::1]:443` would otherwise shed everything after the
    first colon and compare as "[".
    """
    host = origin.split("://", 1)[-1]
    if host.startswith("["):
        return host[1 : host.find("]")].lower() if "]" in host else host.lower()
    return host.split(":")[0].lower()


def collect_production_problems(settings: Settings) -> list[str]:
    """Every reason this configuration must not serve production.

    Returns an empty list when the configuration is acceptable. Split
    from the raising wrapper so tests can assert on individual
    findings without catching exceptions.
    """
    problems: list[str] = []

    # ---- the signing key ------------------------------------------
    secret = settings.JWT_SECRET_KEY or ""
    if _looks_placeholder(secret):
        # The VALUE is never echoed, here or in the message. Naming it
        # would put the secret in the logs of every failed boot.
        problems.append(
            "JWT_SECRET_KEY is a known placeholder. Generate one with: "
            "openssl rand -hex 32"
        )
    elif len(secret) < MIN_JWT_SECRET_CHARS:
        problems.append(
            f"JWT_SECRET_KEY is shorter than {MIN_JWT_SECRET_CHARS} characters"
        )

    # ---- the database ---------------------------------------------
    database_url = settings.DATABASE_URL or ""
    for marker in DEV_DB_MARKERS:
        if marker in database_url:
            problems.append(
                "DATABASE_URL uses development credentials "
                f"({marker.split(':')[0]}); use a generated password"
            )
            break

    # ---- retrieval quality ----------------------------------------
    # passthrough keeps RRF order, whose scores are rank reciprocals
    # rather than relevance -- so the confidence gate cannot threshold
    # them and effectively opens. Shipping that is shipping a system
    # that answers everything.
    if settings.RERANKER_PROVIDER == "passthrough":
        problems.append(
            "RERANKER_PROVIDER=passthrough disables reranking, which opens "
            "the confidence gate; set it to 'hosted'"
        )
    if settings.RERANKER_PROVIDER == "hosted" and _absent(settings.RERANKER_API_KEY):
        problems.append(
            "RERANKER_PROVIDER=hosted but no RERANKER_API_KEY "
            "(COHERE_API_KEY) is set"
        )

    # ---- provider keys --------------------------------------------
    # Presence only. A missing key does not raise at startup today --
    # it surfaces as a failed ingestion or a 500 on the first question.
    if settings.EMBEDDING_PROVIDER == "gemini" and _absent(settings.GEMINI_API_KEY):
        problems.append("EMBEDDING_PROVIDER=gemini but GEMINI_API_KEY is not set")
    if settings.LLM_PROVIDER == "gemini" and _absent(settings.GEMINI_API_KEY):
        problems.append("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set")

    # ---- fakes must never reach production ------------------------
    for name in ("EMBEDDING_PROVIDER", "LLM_PROVIDER", "RERANKER_PROVIDER"):
        if getattr(settings, name) == "fake":
            problems.append(f"{name}=fake is a test provider")

    # ---- spend ----------------------------------------------------
    # 0 means DISABLED, not "zero allowed". Without one of these a
    # single loop against the answer endpoint has no ceiling.
    if not settings.BUDGET_DAILY_GLOBAL_UNITS and not settings.BUDGET_DAILY_ORG_UNITS:
        problems.append(
            "no daily budget is configured (BUDGET_DAILY_GLOBAL_UNITS and "
            "BUDGET_DAILY_ORG_UNITS are both 0, which means disabled)"
        )

    # ---- operational safety ---------------------------------------
    if settings.CELERY_TASK_ALWAYS_EAGER:
        problems.append(
            "CELERY_TASK_ALWAYS_EAGER is set, which runs ingestion inside "
            "the upload request; it is an end-to-end test setting only"
        )
    if not settings.RATE_LIMIT_ENABLED:
        problems.append("RATE_LIMIT_ENABLED is false")

    # ---- cross-origin access --------------------------------------
    # D-3 requires production to refuse an OPEN CORS configuration.
    # What counts as "open" is the judgement, and the bar is
    # deliberately NOT "configured at all": CORS exists because the
    # operator asked for it, and a separate first-party frontend origin
    # is a legitimate production shape. What is refused is credentialed
    # cross-origin access granted to an origin nobody controls.
    #
    # It matters that configure_cors sets allow_credentials=True
    # whenever it is on at all -- so every origin listed here is an
    # origin permitted to send the session cookie and read the reply.
    try:
        cors_origins = settings.cors_allow_origins()
    except ValueError as exc:
        # cors_allow_origins RAISES on a wildcard. Caught rather than
        # allowed to propagate, because this function is a COLLECTOR:
        # an operator fixing a deploy must see every problem in one
        # restart, and a wildcard would otherwise abort the scan and
        # hide everything after it behind an unrelated ValueError.
        cors_origins = []
        problems.append(f"CORS_ALLOW_ORIGINS is invalid: {exc}")

    for origin in cors_origins:
        # Loopback is checked BEFORE the scheme, so the commonest case
        # (`http://localhost:3010`, copied from the dev file) gets the
        # message naming it a development value rather than the generic
        # plaintext one.
        if _origin_host(origin) in LOOPBACK_HOSTS:
            problems.append(
                f"CORS_ALLOW_ORIGINS allows a loopback origin ({origin}); "
                "that is a development value and any process on a user's "
                "machine can serve from it"
            )
        elif not origin.startswith("https://"):
            problems.append(
                f"CORS_ALLOW_ORIGINS allows a non-https origin ({origin}); "
                "credentialed cross-origin access must be over TLS"
            )

    # NOT checked, and named here so the gap is visible rather than
    # silently missing: D-3 also says "if debug is on". There is no
    # DEBUG setting in this application -- FastAPI's own debug flag is
    # never set, and /docs exposure is handled by is_production() at
    # import time in app/main.py. There is nothing to check.

    return problems


def enforce_production_safety(settings: Settings) -> None:
    """Raise UnsafeProductionConfig if this process must not serve.

    A no-op outside production, so a developer's machine is unaffected.
    """
    if not is_production(settings):
        return
    problems = collect_production_problems(settings)
    if problems:
        listed = "\n".join(f"  - {problem}" for problem in problems)
        raise UnsafeProductionConfig(
            f"refusing to start with ENV={settings.ENV}:\n{listed}"
        )

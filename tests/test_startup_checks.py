"""The production configuration gate.

Every other guard in this system degrades on purpose. These EXIT,
because the conditions they catch have no safe degraded mode: a
process signing tokens with the documented placeholder secret lets
anyone who has read the repository mint an admin token for any
organization, and serving at all is the failure.

The tests below are organised around one rule: each check must FIRE on
the bad value and STAY SILENT on the good one. A check that always
fires is noise an operator learns to bypass.
"""
import pytest

from app.core.config import Settings
from app.core.startup_checks import (
    MIN_JWT_SECRET_CHARS,
    UnsafeProductionConfig,
    collect_production_problems,
    enforce_production_safety,
    is_production,
)

GOOD_SECRET = "a" * 64


def _settings(**overrides) -> Settings:
    """A configuration that PASSES, so each test changes one thing.

    Built explicitly rather than from the live settings object: the
    live one has read `.env`, and a developer's local file must never
    be able to make these tests pass or fail.

    That guarantee needs every checked field named here, which is a
    sharper requirement than it looks: `Settings(**base)` still reads
    `.env` for any field the base omits. Adding the CORS check turned
    four of these tests red on this machine, because the local `.env`
    sets CORS_ALLOW_ORIGINS=http://localhost:3010 and the control
    inherited it. The gate was right and the fixture was leaking.
    """
    base = dict(
        ENV="production",
        # Explicitly empty: the deployed architecture is a same-origin
        # BFF that needs no CORS, and leaving this unset would read the
        # developer's `.env` instead.
        CORS_ALLOW_ORIGINS="",
        DATABASE_URL="postgresql://dm:s3cr3t-generated@db:5432/documind",
        JWT_SECRET_KEY=GOOD_SECRET,
        EMBEDDING_PROVIDER="gemini",
        LLM_PROVIDER="gemini",
        RERANKER_PROVIDER="hosted",
        GEMINI_API_KEY="present-not-validated",
        RERANKER_API_KEY="present-not-validated",
        BUDGET_DAILY_GLOBAL_UNITS=500,
        BUDGET_DAILY_ORG_UNITS=100,
        CELERY_TASK_ALWAYS_EAGER=False,
        RATE_LIMIT_ENABLED=True,
    )
    base.update(overrides)
    return Settings(**base)


def test_a_correct_production_config_raises_nothing():
    """The control. Without this, a check that always fires would look
    like a passing suite."""
    assert collect_production_problems(_settings()) == []
    enforce_production_safety(_settings())


# ------------------------------------------------- the signing key ----


@pytest.mark.parametrize(
    "secret",
    [
        "change-me-generate-with-openssl-rand-hex-32",  # ships in .env.example
        "change-me",
        "CHANGE-ME",
        "changeme",
        "change-me-something-else-entirely",
        "secret",
        "your-secret-key-here",
    ],
)
def test_placeholder_jwt_secrets_are_refused(secret):
    problems = collect_production_problems(_settings(JWT_SECRET_KEY=secret))
    assert any("JWT_SECRET_KEY" in p for p in problems), secret


def test_a_short_jwt_secret_is_refused():
    short = "b" * (MIN_JWT_SECRET_CHARS - 1)
    problems = collect_production_problems(_settings(JWT_SECRET_KEY=short))
    assert any("shorter than" in p for p in problems)


def test_the_secret_value_is_never_echoed_in_the_error():
    """A failed boot writes its reason to the logs.

    Naming the offending value would put the secret into the log of
    every failed deploy -- including the real one, the day someone
    sets a short-but-genuine key.
    """
    secret = "b" * 10
    problems = collect_production_problems(_settings(JWT_SECRET_KEY=secret))
    assert problems
    assert all(secret not in p for p in problems)

    with pytest.raises(UnsafeProductionConfig) as caught:
        enforce_production_safety(_settings(JWT_SECRET_KEY=secret))
    assert secret not in str(caught.value)


# ---------------------------------------------------- the database ----


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://documind:documind@db:5432/documind",
        "postgresql://postgres:postgres@db:5432/documind",
    ],
)
def test_development_database_credentials_are_refused(url):
    problems = collect_production_problems(_settings(DATABASE_URL=url))
    assert any("DATABASE_URL" in p for p in problems)


# -------------------------------------------------- the reranker ----


def test_passthrough_reranker_is_refused():
    """The subtle one.

    passthrough is a legitimate configuration -- it keeps fused order
    and answers slightly worse. But fused scores are rank reciprocals,
    not relevance, so the confidence gate cannot threshold them and
    effectively opens. Shipping it means shipping a system that
    answers everything.
    """
    problems = collect_production_problems(_settings(RERANKER_PROVIDER="passthrough"))
    assert any("passthrough" in p for p in problems)


@pytest.mark.parametrize("value", [None, "", "   ", "	"])
def test_hosted_reranker_without_a_key_is_refused(value):
    problems = collect_production_problems(_settings(RERANKER_API_KEY=value))
    assert any("RERANKER_API_KEY" in p for p in problems), value


# ---------------------------------------------- the provider keys ----


# None, "" and "  " are three different values and one condition. The
# suite originally tested only None, and a mutation replacing
# truthiness with `is None` SURVIVED -- so an env file containing a
# bare `GEMINI_API_KEY=` would have booted production.
MISSING_VALUES = [None, "", "   ", "	"]


@pytest.mark.parametrize("value", MISSING_VALUES)
def test_a_missing_gemini_key_is_refused_for_both_uses(value):
    problems = collect_production_problems(_settings(GEMINI_API_KEY=value))
    assert any("EMBEDDING_PROVIDER=gemini" in p for p in problems), value
    assert any("LLM_PROVIDER=gemini" in p for p in problems), value


def test_key_presence_is_checked_but_never_validated():
    """Presence only, deliberately.

    A startup probe that called the provider would make boot depend on
    someone else's uptime and would bill for the privilege. An
    obviously-fake key passes here and fails on first use, which is
    the correct place for it to fail.
    """
    assert collect_production_problems(_settings(GEMINI_API_KEY="x")) == []


@pytest.mark.parametrize(
    "field", ["EMBEDDING_PROVIDER", "LLM_PROVIDER", "RERANKER_PROVIDER"]
)
def test_fake_providers_are_refused(field):
    problems = collect_production_problems(_settings(**{field: "fake"}))
    assert any(field in p and "fake" in p for p in problems)


# ----------------------------------------------------------- spend ----


def test_both_budgets_at_zero_is_refused():
    """0 means DISABLED, not "zero allowed"."""
    problems = collect_production_problems(
        _settings(BUDGET_DAILY_GLOBAL_UNITS=0, BUDGET_DAILY_ORG_UNITS=0)
    )
    assert any("budget" in p.lower() for p in problems)


@pytest.mark.parametrize(
    "overrides",
    [
        {"BUDGET_DAILY_GLOBAL_UNITS": 100, "BUDGET_DAILY_ORG_UNITS": 0},
        {"BUDGET_DAILY_GLOBAL_UNITS": 0, "BUDGET_DAILY_ORG_UNITS": 10},
    ],
)
def test_either_budget_alone_is_enough(overrides):
    assert collect_production_problems(_settings(**overrides)) == []


# -------------------------------------------- operational safety ----


def test_eager_celery_is_refused():
    problems = collect_production_problems(_settings(CELERY_TASK_ALWAYS_EAGER=True))
    assert any("EAGER" in p for p in problems)


def test_disabled_rate_limiting_is_refused():
    problems = collect_production_problems(_settings(RATE_LIMIT_ENABLED=False))
    assert any("RATE_LIMIT_ENABLED" in p for p in problems)


# ------------------------------------------------ scope of the gate ----


@pytest.mark.parametrize("env", ["development", "dev", "local", "test", "staging"])
def test_non_production_is_untouched(env):
    """Development keeps its convenient defaults.

    A gate that fired locally would be switched off within a day, and
    then it would not be there when it mattered.
    """
    hopeless = _settings(
        ENV=env,
        JWT_SECRET_KEY="change-me",
        RERANKER_PROVIDER="passthrough",
        GEMINI_API_KEY=None,
        BUDGET_DAILY_GLOBAL_UNITS=0,
        BUDGET_DAILY_ORG_UNITS=0,
    )
    assert not is_production(hopeless)
    enforce_production_safety(hopeless)  # must not raise


@pytest.mark.parametrize("env", ["production", "PRODUCTION", " Production ", "prod"])
def test_production_is_recognised_however_it_is_spelled(env):
    assert is_production(_settings(ENV=env))


def test_every_problem_is_reported_at_once():
    """An operator fixing a bad deploy should see the whole list in one
    restart, not discover the second problem after fixing the first."""
    problems = collect_production_problems(
        _settings(
            JWT_SECRET_KEY="change-me",
            RERANKER_PROVIDER="passthrough",
            GEMINI_API_KEY=None,
            BUDGET_DAILY_GLOBAL_UNITS=0,
            BUDGET_DAILY_ORG_UNITS=0,
        )
    )
    assert len(problems) >= 4


# ------------------------------------------------- the app itself ----


def test_the_app_disables_docs_in_production(monkeypatch):
    """D-4. Asserted by BUILDING an app, not by reading a flag.

    `docs_url=None` removes the route; it does not merely hide a link,
    so a direct request 404s.
    """
    import importlib

    import app.main as main
    from app.core.config import settings as live

    monkeypatch.setattr(live, "ENV", "production")
    monkeypatch.setattr(live, "JWT_SECRET_KEY", GOOD_SECRET)
    reloaded = importlib.reload(main)
    try:
        assert reloaded.app.docs_url is None
        assert reloaded.app.redoc_url is None
        assert reloaded.app.openapi_url is None
    finally:
        monkeypatch.setattr(live, "ENV", "development")
        importlib.reload(main)


def test_docs_stay_available_outside_production():
    import app.main as main

    assert main.app.docs_url == "/docs"
    assert main.app.openapi_url == "/openapi.json"


# ------------------------------------------------- the WIRING ----


@pytest.mark.asyncio
async def test_the_lifespan_actually_calls_the_gate(monkeypatch):
    """The gate must be CALLED, not merely exist.

    Added because a mutation that deleted the call from the lifespan
    survived the whole suite: every test above exercises the function
    directly, so the function, its tests and its coverage all stay
    green while nothing runs it at startup. That is the failure mode
    where a safety check is present in the repository and absent from
    the deployment.

    Driving the real `lifespan` is the point -- asserting that
    `main.py` contains a particular line would pass against a file
    where the call sits after a `return`.
    """
    import app.main as main
    from app.core.config import settings as live

    monkeypatch.setattr(live, "ENV", "production")
    monkeypatch.setattr(live, "JWT_SECRET_KEY", "change-me")

    with pytest.raises(UnsafeProductionConfig):
        async with main.lifespan(main.app):
            pass  # unreachable: the gate runs before anything else


@pytest.mark.asyncio
async def test_the_lifespan_starts_normally_outside_production(monkeypatch):
    """The control. Without it, a gate that always raised would also
    satisfy the test above."""
    import app.main as main
    from app.core.config import settings as live

    monkeypatch.setattr(live, "ENV", "development")
    monkeypatch.setattr(live, "JWT_SECRET_KEY", "change-me")

    async with main.lifespan(main.app):
        pass


# --------------------------------------------- cross-origin access ----
#
# D-3 lists "or if CORS is open" among the conditions production must
# refuse, and the gate did not check it at all. The bar below is
# deliberately not "configured at all": CORS exists in this backend
# because the operator asked for it, and a separate first-party
# frontend origin is a legitimate production shape. What is refused is
# credentialed cross-origin access handed to an origin nobody controls
# -- and configure_cors sets allow_credentials=True whenever it is on,
# so every listed origin may send the session cookie and read the reply.


def test_no_cors_is_the_passing_case():
    """The control, and the deployed architecture.

    The same-origin BFF needs no CORS at all, so the empty default must
    not be a problem -- otherwise the check is noise on every correct
    deploy and an operator learns to ignore the whole list.
    """
    assert collect_production_problems(_settings(CORS_ALLOW_ORIGINS="")) == []


def test_a_proper_https_origin_is_accepted():
    """A real second origin is allowed. Without this the check would be
    indistinguishable from banning the feature."""
    assert collect_production_problems(
        _settings(CORS_ALLOW_ORIGINS="https://app.example.com")
    ) == []


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:3010",  # the value .env ships for dev
        "https://localhost:3010",
        "http://127.0.0.1:3010",
        "https://127.0.0.1",
        "http://0.0.0.0:8000",
        "https://[::1]:3010",  # the bracketed IPv6 form
    ],
)
def test_loopback_origins_are_refused(origin):
    """A leftover development value, and a real grant.

    Any process on a user's machine can serve from localhost, so this
    permits credentialed calls to production from software the user did
    not install deliberately. `[::1]` is included because an origin
    host has to be unbracketed before the port is split off -- naive
    splitting on ":" compares "[" against the loopback set and lets it
    through.
    """
    problems = collect_production_problems(_settings(CORS_ALLOW_ORIGINS=origin))
    assert any("loopback" in problem for problem in problems), problems


def test_a_plaintext_origin_is_refused():
    """Credentialed CORS over http means the session cookie crosses the
    network in clear text, and HSTS at the edge cannot protect an origin
    that is not this host."""
    problems = collect_production_problems(
        _settings(CORS_ALLOW_ORIGINS="http://app.example.com")
    )
    assert any("non-https" in problem for problem in problems), problems


def test_a_wildcard_is_reported_not_raised():
    """`cors_allow_origins()` RAISES on a wildcard.

    Caught and turned into a finding, because this function is a
    COLLECTOR: an unhandled ValueError would abort the scan and hide
    every later problem behind an error about CORS, which is the
    opposite of "see the whole list in one restart". The condition is
    still refused.
    """
    problems = collect_production_problems(_settings(CORS_ALLOW_ORIGINS="*"))
    assert any("CORS_ALLOW_ORIGINS is invalid" in problem for problem in problems)
    # And it did not swallow the rest of the scan.
    problems = collect_production_problems(
        _settings(CORS_ALLOW_ORIGINS="*", RATE_LIMIT_ENABLED=False)
    )
    assert any("RATE_LIMIT_ENABLED" in problem for problem in problems), problems


def test_one_bad_origin_among_good_ones_is_still_caught():
    """The check is per origin, not on the first entry."""
    problems = collect_production_problems(
        _settings(
            CORS_ALLOW_ORIGINS="https://app.example.com,http://localhost:3010"
        )
    )
    assert any("loopback" in problem for problem in problems), problems

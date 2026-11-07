"""`.env.production.example` is the deployment's instruction sheet.

Three ways it rots, each silent:

1. A new Setting is added and nobody lists it, so an operator reading
   this file believes they have configured everything.
2. A real value is pasted in and committed. The file is the one env
   file that IS tracked, which makes it the likeliest place for a key
   to leak.
3. The example drifts into a configuration that the production gate
   itself refuses -- so following the file exactly produces a process
   that will not boot, and the operator has no way to tell whether the
   file or their edit is wrong.

The last one is why this suite loads the example and runs it through
`collect_production_problems`, rather than only counting lines.
"""
import re
import subprocess
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.startup_checks import collect_production_problems

# Reads repository files that are absent from the runtime image.
pytestmark = pytest.mark.repo

EXAMPLE = Path(__file__).resolve().parents[1] / ".env.production.example"

# Values the example cannot carry, by design. Substituted before the
# file is validated, because a placeholder is the correct content and
# the gate is correct to refuse it.
PLACEHOLDERS = {"<GENERATE>", "<PASTE>"}

# Supplied by docker-compose.prod.yml, not by the env file. The
# example comments them out and says so.
FROM_COMPOSE = {
    "DATABASE_URL": "postgresql://dm:a-real-generated-password@db:5432/documind",
}


def _parse(text: str) -> dict[str, str]:
    """Only UNCOMMENTED assignments -- what the file actually sets."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _all_names(text: str) -> set[str]:
    """Every name assigned, commented or not -- the inventory."""
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))


@pytest.fixture(scope="module")
def text() -> str:
    return EXAMPLE.read_text(encoding="utf-8")


def test_the_example_exists_and_is_tracked():
    """The name claims two properties; it used to assert only one.

    `.gitignore` carries a blanket `.env.*` rule, and the negation for
    this file was missing -- so the example existed on this machine,
    `is_file()` passed, and the file was absent from every clone. This
    module and test_prod_compose's body-limit test both read it, so
    both would have errored in CI, which is the one place P7 needs
    them to run. Asking git directly is the only way to see that:
    presence on disk says nothing about whether it is committed.
    """
    assert EXAMPLE.is_file(), f"{EXAMPLE.name} must exist"

    # --no-index so an uncommitted-but-unignored file still passes:
    # the property under test is "a clone will have it", and `git add`
    # has not necessarily been run at review time.
    result = subprocess.run(
        ["git", "check-ignore", "-q", EXAMPLE.name],
        cwd=EXAMPLE.parent,
        capture_output=True,
    )
    # check-ignore exits 0 when the path IS ignored, 1 when it is not.
    assert result.returncode == 1, (
        f"{EXAMPLE.name} is gitignored, so no clone will have it; "
        "add a `!.env.production.example` negation to .gitignore"
    )


def test_every_setting_appears_somewhere_in_the_file(text):
    """The inventory test.

    Commented-at-its-default counts: the point is that an operator can
    read this one file and see everything the application reads, so a
    new Setting cannot be introduced invisibly.
    """
    named = _all_names(text)
    missing = sorted(n for n in Settings.model_fields if n not in named)
    assert not missing, (
        "new Settings are absent from .env.production.example: "
        + ", ".join(missing)
    )


def test_the_file_names_nothing_the_application_does_not_read(text):
    """The reverse direction, which catches a typo'd variable name.

    A misspelled setting in an env file is inert -- the application
    silently uses the default and the operator believes it was applied.
    """
    known = set(Settings.model_fields) | {
        # Read by docker-compose.prod.yml and deploy/Caddyfile, not by
        # Settings. Compose builds DATABASE_URL and REDIS_URL from the
        # Postgres and Redis values; Caddy reads the hostname.
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "REDIS_PASSWORD",
        "API_HOSTNAME",
        "ACME_EMAIL",
    }
    unknown = sorted(_all_names(text) - known)
    assert not unknown, f"not read by anything: {unknown}"


# ------------------------------------------------- no real secrets ----


@pytest.mark.parametrize(
    "name,pattern",
    [
        ("a Google API key", r"AIza[0-9A-Za-z_\-]{20,}"),
        ("an OpenAI key", r"sk-[0-9A-Za-z]{20,}"),
        ("a long hex string", r"\b[0-9a-f]{32,}\b"),
        ("a long base64 blob", r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
        ("a private key header", r"BEGIN [A-Z ]*PRIVATE KEY"),
    ],
)
def test_no_secret_shaped_string_is_committed(text, name, pattern):
    found = re.findall(pattern, text)
    assert not found, f"{name} appears in the committed example"


def test_every_secret_is_a_placeholder(text):
    """Named explicitly, so adding a secret without a placeholder fails.

    Checking only "nothing looks like a key" would pass a file whose
    JWT_SECRET_KEY line had been deleted outright.
    """
    values = _parse(text)
    for key in ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "JWT_SECRET_KEY",
                "GEMINI_API_KEY", "RERANKER_API_KEY"):
        assert key in values, f"{key} must be present, as a placeholder"
        assert values[key] in PLACEHOLDERS, (
            f"{key} must read a placeholder, not {values[key][:4]!r}..."
        )


# --------------------------------------- the example is a VALID config ----


def test_following_the_example_produces_a_config_that_boots(text):
    """Substitute the placeholders and the production gate must pass.

    Without this the example could quietly contain
    RERANKER_PROVIDER=passthrough or a zero budget -- both legal, both
    refused at boot -- and the first person to discover it would be
    whoever was deploying.
    """
    values = _parse(text)
    filled = {
        k: ("a-generated-value-long-enough-to-pass-the-length-check-0123456789"
            if v in PLACEHOLDERS else v)
        for k, v in values.items()
        if k in Settings.model_fields
    }
    filled.update(FROM_COMPOSE)

    problems = collect_production_problems(Settings(**filled))
    assert problems == [], problems


def test_the_example_sets_production_mode(text):
    assert _parse(text)["ENV"] == "production"


@pytest.mark.parametrize(
    "key,expected,why",
    [
        # D-2: below Caddy's 5MB body limit, so the application refuses
        # an oversized upload with a readable sentence.
        ("MAX_UPLOAD_BYTES", "4000000", "D-2"),
        # D-3: safe ONLY because Caddy is the sole ingress.
        ("TRUST_FORWARDED_FOR", "true", "D-3"),
        # The gate is meaningless on rank-reciprocal scores.
        ("RERANKER_PROVIDER", "hosted", "the confidence gate"),
        # Empty: the BFF is same-origin, so nothing is cross-origin.
        ("CORS_ALLOW_ORIGINS", "", "same-origin BFF"),
    ],
)
def test_the_decided_production_values(text, key, expected, why):
    assert _parse(text).get(key) == expected, why


def test_the_code_default_for_upload_size_was_not_changed():
    """D-2 was explicit: the example only. A developer uploading a 20MB
    PDF locally must not be blocked by a production decision."""
    assert Settings.model_fields["MAX_UPLOAD_BYTES"].default == 25 * 1024 * 1024


def test_at_least_one_budget_is_non_zero(text):
    """0 means disabled. The gate refuses both-at-zero; this pins that
    the example does not merely satisfy it by a hair."""
    values = _parse(text)
    assert int(values["BUDGET_DAILY_GLOBAL_UNITS"]) > 0
    assert int(values["BUDGET_DAILY_ORG_UNITS"]) > 0

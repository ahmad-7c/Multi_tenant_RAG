"""The CI/CD workflows, pinned where a quiet edit would be dangerous.

A workflow file has no type checker and no test of its own, and it only
runs on GitHub -- so a mistake in one is discovered by a bad deploy
rather than by a red local run. These tests read the YAML and assert the
properties that are load-bearing for safety rather than for taste.

Every assertion here corresponds to a specific way this pipeline could
hurt someone: deploying code CI never saw, cancelling a deploy
mid-migration, accepting any SSH host key, interpolating a secret into a
shell line, or deploying a moving tag that cannot be rolled back to.
"""
import re
from pathlib import Path

import pytest

# Reads repository files that are absent from the runtime image.
pytestmark = pytest.mark.repo

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
CD = ROOT / ".github" / "workflows" / "cd.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(workflow: dict):
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            yield step


@pytest.fixture(scope="module")
def ci() -> dict:
    return _load(CI)


@pytest.fixture(scope="module")
def cd() -> dict:
    return _load(CD)


# --------------------------------------------------------- both files --


def test_both_workflows_parse(ci, cd):
    """The control. A workflow with a YAML error does not fail loudly --
    GitHub just never runs it, which looks exactly like a pipeline that
    passed."""
    assert ci["jobs"] and cd["jobs"]


@pytest.mark.parametrize("path", [CI, CD], ids=["ci", "cd"])
def test_no_action_is_used_unpinned(path):
    """A floating ref means the code that runs is whatever the action's
    author pushed last. `@main` in a job holding a deploy key is a
    supply-chain compromise waiting for someone else's bad day."""
    text = path.read_text(encoding="utf-8")
    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses, "no actions found at all -- did the file move?"
    for ref in uses:
        assert "@" in ref, f"{ref} has no version at all"
        version = ref.split("@", 1)[1]
        assert version not in {"main", "master", "HEAD"}, (
            f"{ref} tracks a moving branch"
        )


# ------------------------------------------------------------ ci.yml --


def test_ci_cannot_write_anything(ci):
    """Least privilege, and it matters because CI runs on pull requests.

    CI builds the image to prove it builds and pushes it nowhere.
    `packages: write` belongs only in cd.yml, which does not run on
    untrusted pull-request code.
    """
    assert ci["permissions"] == {"contents": "read"}, ci["permissions"]


def test_ci_runs_the_suite_inside_the_image(ci):
    """R4. The tests must run on the EXACT interpreter and wheels that
    will serve production -- running them on a setup-python runner would
    prove the suite passes somewhere other than the artefact shipped."""
    runs = [s.get("run", "") for s in _steps(ci)]
    pytest_steps = [r for r in runs if "pytest" in r]
    assert pytest_steps, "CI never runs pytest"

    # There are deliberately TWO pytest invocations, and the split is
    # the point. The APPLICATION suite must run inside the image; the
    # `repo`-marked tests read the Dockerfile, the compose file and
    # these workflows, which are absent from the image on purpose, so
    # they run on the checkout instead.
    in_image = [r for r in pytest_steps if "docker run" in r]
    on_runner = [r for r in pytest_steps if "docker run" not in r]

    assert in_image, "the application suite does not run inside the image"
    assert all('-m "not repo"' in r for r in in_image), (
        "the in-image run does not exclude the repo tests: " + repr(in_image)
    )
    # Anything running outside the image may ONLY be the repo subset.
    assert all("-m repo" in r for r in on_runner), (
        "a pytest run outside the image is not restricted to -m repo: "
        + repr(on_runner)
    )


def test_ci_pins_the_image_interpreter(ci):
    """A base-image bump that changes Python must be a failed step, not
    a surprise discovered in production."""
    text = CI.read_text(encoding="utf-8")
    assert "3.14" in text, "CI does not assert the image's Python version"


def test_the_secret_scan_fetches_the_whole_history(ci):
    """A key deleted in a later commit is still in the history and still
    leaked. The default shallow clone cannot see it, so the scan would
    pass while the key sits in a blob anyone can fetch."""
    scan = ci["jobs"]["secrets"]
    checkout = next(s for s in scan["steps"] if "checkout" in str(s.get("uses", "")))
    assert checkout.get("with", {}).get("fetch-depth") == 0


# ------------------------------------------------------------ cd.yml --


def test_cd_only_runs_after_ci_succeeded(cd):
    """`workflow_run` fires on FAILURE too.

    Without an explicit conclusion check a red CI would still deploy,
    which is the opposite of what triggering on CI is for.
    """
    gate = cd["jobs"]["build-push"]["if"]
    assert "workflow_run.conclusion" in gate and "success" in gate, gate


def test_cd_deploys_the_commit_ci_tested(cd):
    """Not whatever main points at now.

    A push landing during the build would otherwise deploy code no CI
    run ever saw -- the race is small and the consequence is unbounded.
    """
    checkout = next(
        s for s in _steps(cd) if "checkout" in str(s.get("uses", ""))
    )
    assert "workflow_run.head_sha" in str(checkout.get("with", {}).get("ref"))


def test_a_deploy_is_never_cancelled_midway(cd):
    """cancel-in-progress would abort a half-finished migration or leave
    the stack between two image tags. A queued deploy must wait."""
    concurrency = cd["concurrency"]
    assert concurrency["cancel-in-progress"] is False, concurrency
    # One group for the whole environment, not per ref: two branches
    # deploying to one box concurrently is the same hazard.
    assert "${{" not in concurrency["group"], concurrency["group"]


def test_the_deploy_host_key_is_verified(cd):
    """StrictHostKeyChecking=no accepts any key presented and makes the
    deploy MITM-able. It is the classic shortcut in exactly this step,
    so its absence is pinned rather than assumed."""
    text = CD.read_text(encoding="utf-8")
    assert "StrictHostKeyChecking=no" not in text
    assert "StrictHostKeyChecking no" not in text
    assert "DEPLOY_KNOWN_HOSTS" in text, "no known_hosts is supplied"


def test_no_secret_is_interpolated_into_a_shell_line(cd):
    """`${{ secrets.X }}` inside a `run:` body is substituted BEFORE the
    shell parses the line, so a secret containing shell metacharacters
    becomes executable text. Secrets must arrive through `env:`."""
    offenders = []
    for step in _steps(cd):
        run = step.get("run", "")
        if "secrets." in run:
            offenders.append(step.get("name", run[:60]))
    assert offenders == [], f"secrets interpolated into run blocks: {offenders}"


def test_the_deployed_tag_is_the_git_sha_not_latest(cd):
    """`latest` moves, so it cannot be rolled back TO.

    It is still pushed as a convenience for humans reading the registry;
    what matters is that the deploy step names the SHA.
    """
    deploy = cd["jobs"]["deploy"]
    text = yaml.safe_dump(deploy)
    assert "outputs.sha" in text, "the deploy step does not reference the SHA"
    assert ":latest" not in text, "the deploy step references a moving tag"


# -------------------------------------------------------- deploy.sh --


def test_deploy_script_is_strict_and_rolls_back():
    """Three properties in one file read.

    Without `set -euo pipefail` a failed `docker pull` would be followed
    by `up -d` on a stale image and the script would report success.
    """
    script = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in script
    assert "rollback()" in script, "there is no rollback function"
    # Health is checked before the smoke test, and a failure of either
    # must call rollback. Pinned by counting the call sites rather than
    # by trusting the order visually.
    assert script.count("rollback") >= 3, "rollback is defined but barely used"


def test_deploy_script_prunes_images_but_never_volumes():
    """The named volumes are every uploaded document and the whole
    database. `docker volume prune` or a `down` with the volume flag in
    a deploy script is an irreversible mistake running unattended."""
    script = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    code = [line for line in script.splitlines() if not line.strip().startswith("#")]
    body = "\n".join(code)
    assert "docker image prune" in body
    assert "volume prune" not in body
    assert "--volumes" not in body
    assert not re.search(r"\bdown\b.*\s-v\b", body)


def test_migrations_run_as_their_own_step_before_the_app_starts():
    """Never inside the api container's start: two containers would race
    the same migration, and a failure would present as a crash-looping
    api rather than a job that exited non-zero."""
    lines = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8").splitlines()
    code = [(i, line) for i, line in enumerate(lines) if not line.strip().startswith("#")]

    migrate_at = next(i for i, line in code if "run --rm migrate" in line)
    # The BARE `up -d`, i.e. the main flow. An earlier naive search
    # matched the one inside rollback() -- which is `up -d --no-deps api
    # worker` and comes first in the file because the function is
    # defined above main. The test failed against a correct script,
    # which is its own small lesson about asserting on substrings.
    up_at = next(i for i, line in code if "$COMPOSE up -d" in line and "--no-deps" not in line)
    assert migrate_at < up_at, "the stack starts before migrations run"


def test_the_rollback_does_not_run_migrations():
    """Expand-then-contract means the previous image reads the newer
    schema, so rolling the code back never needs a schema change. A
    down-migration during an incident is how a bad deploy becomes data
    loss -- so the rollback path must touch migrations not at all."""
    lines = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("rollback() {"))
    # The closing brace of the function, at column 0.
    end = next(i for i in range(start + 1, len(lines)) if lines[i].rstrip() == "}")
    code = [line for line in lines[start:end] if not line.strip().startswith("#")]
    assert not any("migrate" in line for line in code), "the rollback path runs migrations"
    assert not any("alembic" in line for line in code)


def test_the_smoke_test_costs_no_provider_quota():
    """It asks a GREETING on purpose.

    app/services/assistance.py intercepts greetings before the rewriter,
    the cache, the embedder, the reranker and the model, so the path
    exercises auth, the limiter, SSE framing and a database write for
    zero Gemini and zero Cohere spend. A real question would bill on
    every deploy.
    """
    script = (ROOT / "scripts" / "smoke_test.sh").read_text(encoding="utf-8")
    assert '"question":"hello"' in script.replace(" ", "")
    # And it must check readiness, not just liveness: /health reports ok
    # with Postgres unreachable, by design.
    assert "/health/ready" in script

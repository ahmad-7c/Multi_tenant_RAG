"""D-1: the volume-removing compose teardown must not exist in the repo.

The flag in question removes named volumes. Here that is every
uploaded document AND the entire Postgres database, and it is one
character away from the teardown an operator types every day. The ban
is enforced by `scripts/check_no_destructive_commands.sh` rather than
by a comment, because a rule that lives only in a comment is a rule
someone pastes past at 2am.

This module runs that script from the suite, so the ban holds in CI
and in a plain `pytest` rather than only when somebody remembers.

NOTE ON THIS FILE'S OWN TEXT. The forbidden command is never written
out here -- not in a docstring, not in a string literal. It is
assembled from fragments at runtime. The first version spelled it out
and the checker duly flagged this file, which left two options: add a
second name to the checker's exception list, or stop containing the
string. An exception list is the loophole the whole check exists to
close (a real command could hide behind the same excuse), so the
checker still ignores exactly one path -- itself -- and this file
earns no exemption because it needs none.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: A RELATIVE posix path, passed with cwd=ROOT. An absolute Windows
#: path reaches bash as `C:UsersSiliCon...` -- bash reads the
#: backslashes as escapes and reports "No such file or directory",
#: which looks exactly like a missing script.
CHECKER = "scripts/check_no_destructive_commands.sh"

#: Assembled, never written whole. See the module docstring.
FORBIDDEN = "down" + " -" + "v"

# `repo`: scans the repository tree and needs bash plus git, none of
# which exist inside the runtime image.
pytestmark = [
    pytest.mark.repo,
    pytest.mark.skipif(
        shutil.which("bash") is None, reason="the checker is a bash script"
    ),
]


def _run():
    return subprocess.run(
        ["bash", CHECKER], capture_output=True, text=True, cwd=str(ROOT)
    )


def test_the_repo_is_clean():
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_checker_actually_catches_one():
    """Proves the check is not vacuous.

    A grep-based checker that silently stops matching -- a changed
    pattern, a lost `--include` -- passes forever and nothing says so.

    The canary is written INTO the repo rather than a tmp dir on
    purpose: the checker scans the repository, so a file outside it
    would prove only that the checker ignores files it was never
    pointed at. Removed in a `finally`, so a failure here cannot leave
    the canary behind and turn every later run red -- which is exactly
    what happened once when an earlier version of this test was
    interrupted mid-run.
    """
    canary = ROOT / "scripts" / "_destructive_canary.sh"
    try:
        canary.write_text(
            "#!/bin/sh\ndocker compose -f docker-compose.prod.yml " + FORBIDDEN + "\n",
            encoding="utf-8",
        )
        result = _run()
        assert result.returncode == 1, "the checker did not notice the canary"
        assert "_destructive_canary.sh" in result.stdout
    finally:
        canary.unlink(missing_ok=True)

    # And the repo is clean again, so the canary really was the cause.
    assert _run().returncode == 0, "the canary was not cleaned up"


def test_the_checker_ignores_only_itself():
    """One exception, by name, and no others.

    The exception list is the thing that could quietly neuter this
    check: every added name is a place a real command may hide. The
    checker needs exactly one (it contains the pattern by necessity),
    and this asserts the list has not grown.
    """
    script = (ROOT / CHECKER).read_text(encoding="utf-8")
    # Comments are skipped: the script's own commentary discusses why it
    # uses --exclude-dir rather than a trailing filter, and matching that
    # prose made this test fail against a script that was correct.
    code = [
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    ]
    excluded = [line for line in code if "grep -v" in line and "SELF" not in line]
    assert excluded == [], f"the checker gained an exception: {excluded}"

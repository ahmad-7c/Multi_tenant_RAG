"""The backup and restore-drill scripts, pinned where a quiet edit is dangerous.

These cannot be executed here: both need Docker, a running production
stack and a real dump. What CAN be checked without any of that is the
set of properties that make them safe to run unattended on a box whose
named volumes are the only copy of every tenant's data.

Each assertion below corresponds to a specific way a backup job hurts
someone: writing a world-readable dump, reporting success over a
truncated one, deleting files it did not create, or -- the one that
matters most -- a restore drill that touches the production volumes it
exists to avoid.
"""
import re
import stat
from pathlib import Path

import pytest

# Reads repository files that are absent from the runtime image.
pytestmark = pytest.mark.repo

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "scripts" / "backup.sh"
DRILL = ROOT / "scripts" / "restore_drill.sh"

#: Production storage. Neither script may ever write to these, and the
#: drill may not reference them at all.
PRODUCTION_VOLUMES = ("documind-prod_db_data", "documind-prod_uploads")


def _code(path: Path) -> str:
    """The script with comment-only lines removed.

    Both files explain at length what they must never do, so a naive
    substring search finds the prose and not the behaviour -- the same
    trap test_the_smoke_test_calls_routes_that_exist hit.
    """
    stripped = "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )
    # Join backslash continuations, so a command spread over four lines
    # is examined as the one command it is. Without this, a per-line
    # scan reports `find ... -delete` as unscoped because the -maxdepth
    # is on the line above -- the test failing while the script is
    # correct, which is the worst kind of red.
    return re.sub(r"\\\n\s*", " ", stripped)


def test_both_scripts_exist_and_are_shell():
    for path in (BACKUP, DRILL):
        assert path.is_file(), f"{path.name} is missing"
        assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")


def test_both_scripts_fail_fast():
    """`set -euo pipefail`, or a failed step is a silent success.

    Without -e, a failed pg_dump leaves the script running on to the
    retention step, which then deletes a working backup to make room for
    the broken one. Without pipefail, `pg_dump | gzip` reports gzip's
    exit status and a dump that died mid-stream looks fine.
    """
    for path in (BACKUP, DRILL):
        assert "set -euo pipefail" in _code(path), f"{path.name} does not fail fast"


# --------------------------------------------------------------------
# backup.sh
# --------------------------------------------------------------------


def test_the_backup_directory_is_created_private():
    """0700 at creation, not chmod'ed afterwards.

    A dump holds every row of every tenant's data. `mkdir` then `chmod`
    leaves a window where it is group- and world-readable, and on a box
    with more than one account that window is the whole exposure.
    """
    code = _code(BACKUP)
    assert re.search(r"install -d -m 700", code), "the backup dir is not created 0700"
    assert re.search(r"chmod 600\b", code), "the artefacts are not restricted to 600"


def test_the_dump_is_custom_format():
    """-Fc, because the restore drill depends on it.

    Only the custom format can be inspected without restoring
    (`pg_restore --list`) or restored selectively. A plain SQL dump can
    only be replayed whole, which makes both the verification in
    backup.sh and the drill impossible.
    """
    code = _code(BACKUP)
    assert "pg_dump" in code
    assert "-Fc" in code, "the dump is not in custom format"


def test_the_dump_is_verified_not_assumed():
    """A zero-byte dump is the classic silent backup failure.

    The cron job succeeds every night for a year and the first anyone
    knows is a restore that cannot work. `pg_restore --list` parses the
    archive table of contents, so it fails on a truncated or non-archive
    file while touching no database.
    """
    code = _code(BACKUP)
    assert "pg_restore" in code and "--list" in code, "the dump is never verified"
    # And a failed verification must not leave the bad file behind to be
    # mistaken for a backup.
    assert re.search(r"rm -f \"\$DUMP\"", code), "a bad dump is not removed"


def test_the_uploads_archive_mounts_the_volume_read_only():
    """:ro -- a backup must not be able to write to its source.

    Without it a mistyped tar invocation can modify or truncate the
    documents it is reading, which turns a backup job into the incident.
    """
    code = _code(BACKUP)
    assert "documind-prod_uploads:/src:ro" in code, "the source volume is writable"


def test_retention_cannot_delete_what_it_did_not_create():
    """Scoped by name, by type and to one directory level.

    An unscoped `find -delete` under a mistyped BACKUP_DIR is a
    data-loss bug in a script whose whole job is preventing data loss.
    """
    code = _code(BACKUP)
    deletes = [line for line in code.splitlines() if "-delete" in line]
    assert deletes, "nothing prunes old backups"
    for line in deletes:
        assert "-maxdepth 1" in line, f"unscoped depth: {line.strip()}"
        assert "-type f" in line, f"not restricted to files: {line.strip()}"
        assert "-name 'documind-" in line, f"unscoped name: {line.strip()}"
        assert "-mtime +" in line, f"no age filter: {line.strip()}"


def test_the_backup_never_reads_the_database_password():
    """pg_dump runs inside the container, over the trusted local socket.

    Reading POSTGRES_PASSWORD out of .env.production would put it in the
    script's environment and therefore in the cron job's environment,
    for no benefit: the postgres image trusts local connections, so the
    password is not needed at all.
    """
    code = _code(BACKUP)
    assert "POSTGRES_PASSWORD" not in code, "the backup reads the DB password"
    assert "PGPASSWORD" not in code


# --------------------------------------------------------------------
# restore_drill.sh -- the property that matters
# --------------------------------------------------------------------


def test_the_drill_never_names_a_production_volume():
    """The whole safety argument, made structural.

    The drill restores a dump. If it could address the production data
    directory, a mistake in it would overwrite the database with an
    older copy -- destroying exactly what it was run to protect. It must
    not be able to name the volume at all.
    """
    code = _code(DRILL)
    for volume in PRODUCTION_VOLUMES:
        assert volume not in code, f"the drill references {volume}"


def test_the_drill_container_is_disposable():
    """Its own anonymous volume, no published port, always destroyed.

    No `-v` means the data directory dies with the container, so a
    second drill cannot inherit the first one's state and report a pass
    over stale data. No `-p` means a database with a throwaway password
    is never reachable from outside the host.
    """
    code = _code(DRILL)
    run_lines = [l for l in code.splitlines() if "docker run -d" in l]
    assert run_lines, "the drill never starts a container"
    line = run_lines[0]
    assert "--rm" in line, "the drill container is not removed on exit"
    assert " -p " not in line and "--publish" not in line, "the drill publishes a port"
    assert re.search(r"-v\s", line) is None, "the drill mounts a volume"
    # And a trap, so a failure mid-drill still cleans up.
    assert "trap cleanup EXIT" in code, "a failed drill leaks its container"


def test_the_drill_reads_production_but_never_writes_it():
    """Counts come from production; nothing is sent back.

    The only production access is `psql -tAc "select count(*) ..."`.
    Anything that is not a select would make the drill a writer.
    """
    code = _code(DRILL)
    prod_calls = [l for l in code.splitlines() if "$COMPOSE exec" in l]
    assert prod_calls, "the drill never reads production counts"
    for line in prod_calls:
        assert "select count(*)" in line, f"non-select against production: {line.strip()}"
    # pg_restore must never be aimed at the production stack.
    for line in code.splitlines():
        if "pg_restore" in line:
            assert "$COMPOSE" not in line, "pg_restore is pointed at production"


def test_the_drill_fails_loudly_when_the_restore_is_wrong():
    """It must be able to FAIL. A drill that always passes proves nothing.

    Three conditions are failures: a table missing from the restore,
    more rows than production has (impossible from a dump of it), and a
    table empty in the restore but populated in production. Fewer rows
    than production is NOT a failure -- production keeps accepting
    writes after the dump is taken.
    """
    code = _code(DRILL)
    assert "MISSING TABLE" in code
    assert "MORE THAN PROD" in code
    assert "EMPTY, PROD IS NOT" in code
    assert re.search(r"fail=1", code), "nothing ever sets a failure flag"
    assert re.search(r'\[ "\$fail" -ne 0 \]', code), "the failure flag is never acted on"
    # The schema version is checked too: a dump restored into the wrong
    # code generation is a different kind of broken that counts cannot see.
    assert "alembic_version" in code


def test_the_drill_creates_the_vector_extension_before_restoring():
    """document_chunks has a vector column.

    Without `create extension vector` the restore fails on an unknown
    type, and the drill would report a broken backup when the backup is
    fine -- which is worse than no drill, because it sends you looking
    in the wrong place.
    """
    code = _code(DRILL)
    create_at = code.index("create extension if not exists vector")
    restore_at = code.index("pg_restore")
    assert create_at < restore_at, "the extension is created after the restore"


def test_neither_script_is_referenced_before_it_is_documented():
    """The runbook must actually tell someone how to run these.

    A backup script nobody installs is not a backup, and the crontab
    line is the only part that makes it nightly.
    """
    runbook = (ROOT / "deploy" / "runbook.md").read_text(encoding="utf-8")
    assert "scripts/backup.sh" in runbook
    assert "scripts/restore_drill.sh" in runbook
    assert "crontab" in runbook, "nothing schedules the backup"

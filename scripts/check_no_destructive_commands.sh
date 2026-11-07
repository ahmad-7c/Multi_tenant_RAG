#!/usr/bin/env bash
# Fail if any script or document in the repo contains a command that
# would destroy production data.
#
# D-1 requires that `docker compose down -v` never appears in deploy.sh
# or the docs. `-v` removes named volumes, which here is every uploaded
# document AND the entire Postgres database. It is one character away
# from the `down` an operator types every day, and it is irreversible.
#
# This is a check, not a convention: a rule that lives only in a
# comment is a rule someone will paste past at 2am.
set -euo pipefail

cd "$(dirname "$0")/.."

# `down` followed by -v / --volumes on the same line, in any order of
# flags. Also catches `docker-compose` (the old binary name).
PATTERN='docker[- ]compose[^|;]*\bdown\b[^|;]*(-v\b|--volumes\b)'

# The checker itself and the file documenting the ban both contain the
# phrase by necessity.
SELF="scripts/check_no_destructive_commands.sh"

# --exclude-dir, not a `grep -v` afterwards: the post-filter still READ
# every file under web/node_modules and .next before discarding them,
# which took ~40s on this tree and made the test that wraps this script
# too slow to keep. Excluding up front is the same guarantee for a
# fraction of the I/O.
HITS=$(grep -rInE "$PATTERN" \
        --include='*.sh' --include='*.yml' --include='*.yaml' \
        --include='*.md' --include='Makefile' --include='*.py' \
        --exclude-dir=venv --exclude-dir=.venv \
        --exclude-dir=node_modules --exclude-dir=.next \
        --exclude-dir=.git --exclude-dir=__pycache__ \
        . 2>/dev/null \
      | grep -v "^./$SELF:" \
      || true)

if [ -n "$HITS" ]; then
  echo "FAIL: a destructive volume-removing command is present."
  echo "      'down -v' deletes the uploads and db_data volumes."
  echo
  echo "$HITS" | sed 's/^/  /'
  exit 1
fi

echo "OK: no 'docker compose down -v' anywhere in the repo."

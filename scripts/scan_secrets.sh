#!/usr/bin/env bash
# Secret scan: every tracked file, and every blob in the git history.
#
# Deliberately NOT a third-party action. gitleaks and trufflehog would
# each have to be pinned to a commit SHA this repo cannot verify
# offline, and an unpinned action in a job that reads the whole history
# is the supply-chain risk the pinning rule exists to prevent. This is a
# short auditable script instead -- see docs/adr/0007 for the tradeoff
# and when to revisit it.
set -euo pipefail

cd "$(dirname "$0")/.."

# TRACKED FILES ONLY. The working tree holds `.env` with a real
# password-bearing DSN; it is gitignored, so it is not in the repo, not
# in the image and not in a CI checkout. Scanning it would fail every
# local run over a file that cannot leak, and a check that cries wolf
# gets switched off. `git ls-files` is also the honest definition of
# "in the repo": it is exactly what a clone receives.
tracked_files() { git ls-files -z; }

# A control string that IS present, so a silently-broken grep is
# visible rather than reported as a clean tree. U6 learned this one the
# hard way: a scan matching nothing looks identical to a clean result.
CONTROL="EMBEDDING_PROVIDER"

# Hard secrets. Any match is a failure, no exceptions.
PATTERNS=(
  "AIza[0-9A-Za-z_-]{30,}"
  "sk-[A-Za-z0-9_-]{20,}"
  "sk_live_[A-Za-z0-9]{10,}"
  "-----BEGIN [A-Z ]*PRIVATE KEY"
)

# A DSN carrying a password is handled separately, because the repo
# legitimately contains several. `postgresql://documind:documind@...`
# is the DOCUMENTED development default -- it is in .env.example and
# docker-compose.yml on purpose, it is published in CLAUDE.md, and it
# is useless to anyone who is not already on this laptop.
#
# These are not an exception list in the loophole sense: they are the
# same credentials app/core/startup_checks.py::DEV_DB_MARKERS already
# REFUSES to boot with in production. The scan allows them because a
# second control makes them harmless; anything NOT on this list still
# fails, which is the case that matters -- a production DSN pasted into
# a tracked file.
DSN_PATTERN="(postgres(ql)?|redis)://[^[:space:]\"'"'"']*:[^[:space:]@\"'"'"']+@"
KNOWN_DEV_CREDS="documind:documind|postgres:postgres|user:password|unused:unused|:\\$\{|<PASTE>|<GENERATE>"

fail=0

echo "== control: the scanner is actually matching"
if tracked_files | xargs -0 grep -lIE "$CONTROL" >/dev/null 2>&1; then
  echo "  OK: control string found, grep works"
else
  echo "  FAIL: control string not found -- the scan is broken, not clean"
  exit 1
fi

report() { # $1 = label, $2 = hits
  echo "  FAIL: $1"
  # The MATCH is never printed, only its location. Printing it would put
  # the secret into the CI log, which is itself a disclosure.
  echo "$2" | cut -d: -f1,2 | sort -u | sed "s/^/    /"
  fail=1
}

echo "== tracked files: hard secrets"
for pattern in "${PATTERNS[@]}"; do
  hits=$(tracked_files | xargs -0 grep -nIE "$pattern" 2>/dev/null || true)
  [ -n "$hits" ] && report "pattern matched: $pattern" "$hits"
done

echo "== tracked files: credential-bearing URLs"
hits=$(tracked_files | xargs -0 grep -nIE "$DSN_PATTERN" 2>/dev/null        | grep -vE "$KNOWN_DEV_CREDS" || true)
[ -n "$hits" ] && report "a URL carries a non-development password" "$hits"

echo "== git history: every blob ever committed"
# A key deleted in a later commit is still in the history and still
# leaked. --all covers every ref, not only the current branch.
for pattern in "${PATTERNS[@]}"; do
  hits=$(git grep -InIE "$pattern" $(git rev-list --all) 2>/dev/null | head -20 || true)
  [ -n "$hits" ] && report "pattern in history: $pattern" "$hits"
done

echo
if [ "$fail" -ne 0 ]; then
  echo "SECRET SCAN FAILED. A key that was ever committed must be treated"
  echo "as LEAKED and ROTATED -- deleting it from the tree is not enough,"
  echo "because the blob survives in the history."
  exit 1
fi
echo "SECRET SCAN PASSED"

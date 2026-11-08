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

echo "== credential-bearing URLs (excluding tests and CI config)"
# SCOPED, and the scope is the whole reason this rule is useful.
#
# Test fixtures and CI config contain DSNs with invented passwords SO
# THAT the production gate's dev-credential check can be tested at all:
# startup_checks.DEV_DB_MARKERS refuses documind:documind and friends,
# so proving it ACCEPTS a real-looking one needs a fixture that looks
# real. The scanner cannot tell that from shape, and it flagged three
# such lines the moment 7ed43ae made them tracked:
#
#   tests/test_env_production_example.py:40
#   tests/test_startup_checks.py:47
#   .github/workflows/ci.yml   (a one-character user and password)
#
# None of those three is quoted verbatim here, and that is not
# squeamishness: an earlier version of this comment pasted the CI one in
# full, and the scanner flagged ITSELF -- scripts/ is not on the
# exclusion list, so the documentation of an excluded example became a
# finding. The rule was right and the comment was wrong.
#
# What is NOT narrowed: every pattern in PATTERNS above (API keys,
# private keys) still applies to EVERY tracked file, tests included. A
# real Gemini key pasted into a test still fails this scan. Only the
# DSN heuristic is scoped, because a Postgres password is the one
# credential this deployment already defends elsewhere -- `.env` is
# gitignored, `.env.production.example` is asserted to hold only
# placeholders, and startup_checks refuses dev credentials at boot.
#
# Measured rather than assumed. A probe staged under app/ carrying both
# a key-shaped string and a production-shaped DSN was caught by this
# narrowed scanner on both rules:
#
#   FAIL: pattern matched: AIza[0-9A-Za-z_-]{30,}   app/_scan_probe.py:1
#   FAIL: a URL carries a non-development password  app/_scan_probe.py:2
#
# so the exclusion covers the two fixture directories and nothing else.
hits=$(tracked_files        | xargs -0 grep -nIE "$DSN_PATTERN" 2>/dev/null        | grep -vE "^(tests/|\.github/)"        | grep -vE "$KNOWN_DEV_CREDS" || true)
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

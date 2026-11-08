#!/usr/bin/env bash
# Run the smoke test WITH the deep check, as a separate invocation.
#
#   printf '%s\n%s\n%s\n' "$ORG" "$EMAIL" "$PASSWORD" | ./scripts/smoke_deep.sh
#
# WHY THIS IS NOT PART OF deploy.sh. deploy.sh treats a failed smoke as
# grounds to ROLL BACK. The deep check spends provider quota and can
# fail because Gemini or Cohere is having a bad day -- and rolling back
# would not fix that, because the previous image cannot reach them
# either. A blocking deep check therefore converts someone else's outage
# into a reverted deploy plus a misleading alert.
#
# So CD runs this AFTER the deploy is recorded, as a reporting step. The
# deploy's own gate stays checks 1-6, which cost nothing and cover
# everything a deploy can actually break: TLS, the edge, auth, the
# limiter, SSE framing, a database write and the migrations.
set -euo pipefail

cd "$(dirname "$0")/.."

# Same contract as deploy.sh: three lines on stdin, read before anything
# else can consume them. Not arguments -- `ssh host "cmd $SECRET"` puts
# the value in the process list on both ends.
SMOKE_ORG=""
SMOKE_EMAIL=""
SMOKE_PASSWORD=""
if [ ! -t 0 ]; then
  IFS= read -r SMOKE_ORG      || true
  IFS= read -r SMOKE_EMAIL    || true
  IFS= read -r SMOKE_PASSWORD || true
fi
exec < /dev/null
export SMOKE_ORG SMOKE_EMAIL SMOKE_PASSWORD

# The real hostname. Caddy serves exactly one site address, so the
# script's https://localhost default presents an SNI no site matches.
SMOKE_BASE_URL="https://$(sed -n 's/^API_HOSTNAME=//p' .env.production | head -1)"
export SMOKE_BASE_URL
if [ "$SMOKE_BASE_URL" = "https://" ]; then
  echo "API_HOSTNAME is not set in .env.production" >&2
  exit 1
fi

export SMOKE_DEEP=1
exec ./scripts/smoke_test.sh

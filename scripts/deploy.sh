#!/usr/bin/env bash
# Deploy one image SHA, verify it, and roll back by itself if it fails.
#
#   ./scripts/deploy.sh ghcr.io/owner/repo <git-sha>
#
# Run ON THE SERVER, from /opt/documind. cd.yml invokes it over ssh,
# but it is a plain script on purpose: during an incident GitHub may be
# the thing that is down, and the recovery path must not depend on it.
#
# ROLLBACK IS CODE ROLLBACK ONLY, never a database rollback. Migrations
# are expand-then-contract, so the previous image can always read the
# newer schema. A down-migration during an incident is how a bad deploy
# becomes data loss.
set -euo pipefail

IMAGE="${1:?usage: deploy.sh <image> <sha>}"
SHA="${2:?usage: deploy.sh <image> <sha>}"

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.production"

# The tag currently live. Written only AFTER a deploy verifies, so this
# file always names a revision that actually passed its checks -- which
# is the whole requirement for a rollback target.
STATE_FILE=".deployed_sha"
PREVIOUS="$(cat "$STATE_FILE" 2>/dev/null || true)"

log() { echo "[deploy $(date -u +%H:%M:%S)] $*"; }

# ------------------------------------------------- smoke credentials
# Three lines on STDIN, in order: org, email, password.
#
# NOT arguments: an argument is visible in `ps` to every account on the
# box for the life of the process, and `ssh host "cmd $SECRET"` puts it
# there on BOTH ends -- in the runner's process list and in the server's.
#
# NOT .env.production either: that file is application config read by
# five containers, and these are CI's test account. Putting them there
# would widen what a container compromise reaches for no benefit.
#
# Read BEFORE anything else runs, because `docker compose run --rm
# migrate` below inherits stdin and would consume these lines. Once they
# are read, stdin is closed for the rest of the script.
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

# The smoke test must address the REAL hostname. Caddy serves exactly
# one site address ({$API_HOSTNAME}), so the script's https://localhost
# default presents an SNI no site matches -- the smoke would fail for a
# reason that has nothing to do with the deploy. API_HOSTNAME is not a
# secret; nothing else is read out of that file here.
SMOKE_BASE_URL="https://$(sed -n 's/^API_HOSTNAME=//p' .env.production | head -1)"
export SMOKE_BASE_URL
if [ "$SMOKE_BASE_URL" = "https://" ]; then
  log "API_HOSTNAME is not set in .env.production -- cannot address the smoke test"
  exit 1
fi

# ---------------------------------------------------------------- wait
wait_for_api_health() {
  # Polls the CONTAINER healthcheck rather than curling a port. The api
  # publishes none (only caddy does), and the healthcheck already hits
  # /health/ready, which round-trips SELECT 1 -- so "healthy" here means
  # the database is reachable, not merely that the process is up.
  local cid status
  cid="$($COMPOSE ps -q api)"
  if [ -z "$cid" ]; then log "api container does not exist"; return 1; fi
  for _ in $(seq 1 60); do
    status="$(docker inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo unknown)"
    case "$status" in
      healthy)   log "api is healthy"; return 0 ;;
      unhealthy) log "api reported UNHEALTHY"; return 1 ;;
    esac
    sleep 5
  done
  log "api did not become healthy within 300s (last status: $status)"
  return 1
}

# ------------------------------------------------------------ rollback
rollback() {
  if [ -z "$PREVIOUS" ]; then
    log "NO PREVIOUS SHA RECORDED -- cannot roll back automatically."
    log "This is the first deploy, or $STATE_FILE was lost. The stack is"
    log "left as it is, deliberately: guessing a tag would be worse than"
    log "stopping with a clear message."
    return 1
  fi
  log "ROLLING BACK to $PREVIOUS"
  # No migrate job on the way back: expand-then-contract means the old
  # code reads the new schema. A down-migration here is what turns a
  # failed deploy into an outage with data loss.
  API_IMAGE="$IMAGE:$PREVIOUS" $COMPOSE up -d --no-deps api worker
  if wait_for_api_health; then
    log "rollback to $PREVIOUS is healthy"
  else
    log "ROLLBACK ALSO FAILED -- manual intervention required"
  fi
  return 1
}

# ---------------------------------------------------------------- main
log "deploying $IMAGE:$SHA (previous: ${PREVIOUS:-none})"

# Pull BY SHA before touching the running stack, so a registry problem
# fails before anything is stopped.
log "pulling"
docker pull "$IMAGE:$SHA"

export API_IMAGE="$IMAGE:$SHA"

# Migrations as a SEPARATE one-shot job, before api and worker restart.
# Never inside the api container's start: two containers would race the
# same migration, and a failed migration would look like a crash-looping
# api rather than a job that exited non-zero.
log "running migrations"
if ! $COMPOSE run --rm migrate; then
  log "MIGRATION FAILED -- the running stack was not touched"
  # Nothing to roll back: api and worker are still on the old image.
  exit 1
fi

log "starting the new image"
$COMPOSE up -d

if ! wait_for_api_health; then
  rollback
  exit 1
fi

log "running the smoke test"
if ! ./scripts/smoke_test.sh; then
  log "SMOKE TEST FAILED"
  rollback
  exit 1
fi

# Only now is this SHA a valid rollback target.
echo "$SHA" > "$STATE_FILE"
log "DEPLOYED $SHA"

# Old images accumulate and the box has one small disk. This keeps the
# previous tag so a rollback is still possible and prunes the rest.
# NOTE: images only -- never volumes. The named volumes here hold every
# uploaded document and the entire database.
log "pruning old images (volumes are never touched)"
docker image prune -f --filter "until=168h" >/dev/null 2>&1 || true

log "done"

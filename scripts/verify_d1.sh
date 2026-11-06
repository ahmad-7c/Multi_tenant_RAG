#!/usr/bin/env bash
# D1 verification: everything P1 and P2 claim, proven by running it.
#
# Written to be run by hand because the agent's Docker daemon wedged
# mid-build. Each step PRINTS what it proves and fails the script if the
# property does not hold. Nothing here prints a secret: values are
# checked for presence and shape, never echoed.
#
#   bash scripts/verify_d1.sh
#
# Prerequisite: a LOCAL .env.production. It is gitignored and must stay
# that way. Generate the two passwords and the signing key with:
#
#   openssl rand -hex 32     # JWT_SECRET_KEY
#   openssl rand -hex 24     # POSTGRES_PASSWORD
#   openssl rand -hex 24     # REDIS_PASSWORD
#
# and copy your own provider keys in. The production gate requires
# GEMINI_API_KEY and RERANKER_API_KEY present, RERANKER_PROVIDER=hosted,
# and at least one non-zero budget -- it will refuse to boot otherwise,
# which is the point of it.
set -euo pipefail

# NOTE on `$(... || true)` below. With `pipefail`, a command
# substitution whose command exits non-zero aborts the whole script --
# including when non-zero IS the result we want. `ls -d /code/tests`
# returning 2 because the path is absent is the property under test,
# and it killed this script at step 3 before the guards were added.

cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.production"
PASS=0
FAIL=0

ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
step() { echo; echo "== $1"; }

step "0. prerequisites"
if [ ! -f .env.production ]; then
  echo "  .env.production is missing -- see the header of this script."
  exit 1
fi
ok ".env.production exists (contents never printed)"
# Mode check is informational on Windows, where chmod bits do not apply.
if command -v stat >/dev/null 2>&1; then
  echo "  mode: $(stat -c '%a' .env.production 2>/dev/null || echo 'n/a on this platform')"
fi

step "1. R4 -- build the image on the SAME Python the tests run on"
docker build --target runtime -t documind-api:local .
docker build --target test    -t documind-api:test  .
IMG_PY=$($COMPOSE run --rm --no-deps --entrypoint python api -c   'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null | tr -d '[:cntrl:]' || true)
LOCAL_PY=$(python -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null | tr -d '[:cntrl:]' || echo "?")
echo "  image python: $IMG_PY   local python: $LOCAL_PY"
[ "$IMG_PY" = "3.14" ] && ok "image runs Python 3.14" || bad "image python is $IMG_PY, expected 3.14"

step "2. R4 -- the FULL test suite inside the image"
# Against the dev test-db on the host, reachable as host.docker.internal.
# conftest refuses to run unless TEST_DATABASE_URL differs from
# DATABASE_URL, so both are supplied and deliberately different.
docker run --rm   -e TEST_DATABASE_URL="postgresql://documind:documind@host.docker.internal:5433/documind_test"   -e DATABASE_URL="postgresql://unused:unused@127.0.0.1:1/unused"   -e JWT_SECRET_KEY="test-only-not-a-real-secret"   documind-api:test python -m pytest -q -m "not repo"   && ok "full suite green inside the image"   || bad "the suite failed inside the image"

step "3. .dockerignore -- no .env and no uploads in the image"
# Listed from the image filesystem, not inferred from the ignore file.
LEAKED=$(docker run --rm --entrypoint sh documind-api:local -c   'find / -xdev \( -name ".env" -o -name ".env.*" \) -not -name "*.example" 2>/dev/null;    find /code/uploads -mindepth 1 2>/dev/null' | tr -d '[:cntrl:]' || true)
if [ -z "$LEAKED" ]; then ok "no .env file and no uploaded documents in the image"
else bad "the image contains: $LEAKED"; fi

# And nothing in any LAYER, which a later deletion would otherwise hide.
if docker history --no-trunc documind-api:local | grep -qiE '\.env([^.]|$)'; then
  bad "docker history mentions a .env copy"
else
  ok "no layer in docker history copies a .env"
fi

# tests/ and eval/ are in the build context (the test stage needs them)
# but must not be in the runtime artefact.
STRAY=$(docker run --rm --entrypoint sh documind-api:local -c   'ls -d /code/tests /code/eval 2>/dev/null' | tr -d '[:cntrl:]' || true)
[ -z "$STRAY" ] && ok "tests/ and eval/ are absent from the runtime stage"                 || bad "runtime stage contains: $STRAY"

step "4. P1 -- bring the stack up with the prod compose file"
$COMPOSE up -d
echo "  waiting for health..."
# Only services that HAVE a healthcheck are waited on. caddy and the
# one-shot migrate job have none, so `{{.Health}}` is empty for them --
# and a filter of `grep -v healthy` keeps those lines, which made this
# loop spin for its full timeout against a stack that was already up.
for _ in $(seq 1 40); do
  PENDING=$($COMPOSE ps --format '{{.Name}} {{.Health}}'             | awk 'NF==2 && $2!="healthy"' || true)
  [ -z "$PENDING" ] && break
  sleep 5
done
$COMPOSE ps --format '  {{.Name}} | {{.Status}} | {{.Ports}}'

step "5. P1 -- ONLY caddy publishes ports; db and redis publish nothing"
for svc in db redis api worker migrate; do
  PORTS=$($COMPOSE ps --format '{{.Name}} {{.Ports}}' | grep -E "[-_]${svc}[-_]" | sed 's/^[^ ]* //' || true)
  if echo "$PORTS" | grep -qE '0\.0\.0\.0|:::|->'; then
    bad "$svc publishes a port: $PORTS"
  else
    ok "$svc publishes nothing"
  fi
done
CADDY_PORTS=$($COMPOSE ps --format '{{.Name}} {{.Ports}}' | grep -E '[-_]caddy[-_]' || true)
echo "  caddy: $CADDY_PORTS"
echo "$CADDY_PORTS" | grep -q '80->80'   && ok "caddy publishes 80"  || bad "caddy does not publish 80"
echo "$CADDY_PORTS" | grep -q '443->443' && ok "caddy publishes 443" || bad "caddy does not publish 443"

# Proven from OUTSIDE as well: a direct connection to the datastore
# ports must be refused on the host.
# Scoped to THIS compose project, not to the host. A dev stack on the
# same machine publishes 6379, so a bare host probe reported a failure
# that had nothing to do with docker-compose.prod.yml -- the prod redis
# correctly published nothing. On the server there is no dev stack and
# the external scan in P10 is the real outside check.
for svc in db redis; do
  BOUND=$($COMPOSE ps --format '{{.Name}} {{.Ports}}' | grep -E "[-_]${svc}[-_]" | grep -oE '0\.0\.0\.0:[0-9]+|:::[0-9]+' || true)
  if [ -n "$BOUND" ]; then
    bad "$svc is bound on the host: $BOUND"
  else
    ok "$svc has no host binding in this project"
  fi
done

step "6. D-1 -- the uploads volume is writable by the NON-ROOT user"
WHO=$($COMPOSE exec -T api id -u | tr -d '[:cntrl:]' || true)
[ "$WHO" = "10001" ] && ok "api runs as uid 10001, not root" || bad "api runs as uid $WHO"
$COMPOSE exec -T api sh -c 'echo d1-probe > /code/uploads/.d1-probe && cat /code/uploads/.d1-probe'   >/dev/null && ok "api can write to /code/uploads" || bad "api cannot write to /code/uploads"

step "7a. the worker is actually RUNNING"
# Checked explicitly and BEFORE the volume test. A crash-looping worker
# makes the volume test fail with "worker cannot see the api's uploads",
# which blames the volume wiring for what was really a dependency
# mismatch (kombu 5.7.0 vs redis-py 5.0.8). Wrong cause, wrong fix.
WSTATE=$($COMPOSE ps --format '{{.Name}} {{.Status}}' | grep -E '[-_]worker[-_]' || true)
echo "  $WSTATE"
case "$WSTATE" in
  *Restarting*|*Exited*) bad "the worker is not running: $WSTATE" ;;
  *Up*)                  ok "the worker is up" ;;
  *)                     bad "cannot determine worker state: $WSTATE" ;;
esac

step "7. D-1 -- api and worker share the SAME uploads volume"
# The file the api just wrote must be visible to the worker. Separate
# volumes would make every ingestion fail on a missing file, and the
# symptom would be a failed document rather than a config error.
$COMPOSE exec -T worker sh -c 'cat /code/uploads/.d1-probe' | grep -q d1-probe   && ok "worker reads the file the api wrote" || bad "worker cannot see the api's uploads"
$COMPOSE exec -T api sh -c 'rm -f /code/uploads/.d1-probe' || true

step "8. P2 -- the edge, HTTPS, and the forged-header test (D-3)"
curl -ksS -o /dev/null -w '  https://localhost/health -> %{http_code}
' https://localhost/health
# HTTP must redirect to HTTPS (Caddy does this automatically for a
# hostname site address; verified in /docs/automatic-https).
RED=$(curl -s -o /dev/null -w '%{http_code}' http://localhost/health || true)
case "$RED" in 30*) ok "http redirects (status $RED)";; *) bad "http did not redirect (status $RED)";; esac
# No Server header.
if curl -ksSI https://localhost/health | grep -qi '^server:'; then
  bad "the edge advertises a Server header"
else
  ok "no Server header at the edge"
fi

step "9. D-3 -- a forged X-Forwarded-For must not be used"
EDGE_BASE_URL=https://localhost ./venv/Scripts/python.exe -m pytest tests/test_edge_live.py -q -p no:warnings   && ok "forged X-Forwarded-For is ignored (limiter still trips)"   || bad "the forged-header test failed"

echo
echo "================ D1: $PASS passed, $FAIL failed ================"
echo "Tear down with:  $COMPOSE down        (NEVER with the volume flag)"
[ "$FAIL" -eq 0 ] || exit 1

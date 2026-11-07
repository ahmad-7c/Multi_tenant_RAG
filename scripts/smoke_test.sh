#!/usr/bin/env bash
# Post-deploy smoke test. Exercises the real stack through the edge and
# COSTS NOTHING in provider quota.
#
# deploy.sh calls it and rolls back if it fails.
#
# WHY A GREETING IS THE QUESTION IT ASKS. The spec wants "one cached or
# fake-provider question path that costs no provider budget". In
# production the providers are real, and a cached question cannot be
# relied on: a fresh deploy may have a cold cache, and the cache key
# includes the org's index_version. But app/services/assistance.py
# intercepts greetings BEFORE the rewriter, the cache, the embedder, the
# reranker and the model -- a test measures zero embedder calls, zero
# rerank calls and zero LLM calls on that path. So "hello" exercises
# TLS, the edge, auth, the rate limiter, SSE framing and a database
# write while spending nothing with Gemini or Cohere.
#
# It therefore does NOT prove retrieval or generation work. That is a
# deliberate limit, stated here rather than implied: proving those costs
# provider quota on every deploy, and P10's verification list is where
# that belongs.
set -euo pipefail

# ---- credentials are REQUIRED, and missing ones are a FAILURE --------
#
# This used to print "SKIP login" and carry on exiting 0, so a deploy
# whose credentials never arrived was reported as a successful smoke.
# A skipped check must never count as a pass: deploy.sh reads this
# script's exit status as "production works", and the two checks that
# depend on these variables are the only ones that exercise auth, the
# answer path and a database write.
#
# Checked FIRST, before any network call, so the failure names the real
# cause instead of surfacing as a confusing HTTP error.
#
# Fail-closed by default: a local run that genuinely wants the short
# version must say so with SMOKE_ALLOW_SKIP=1. The dangerous direction
# is the one that needs the explicit opt-in.
missing=""
for var in SMOKE_ORG SMOKE_EMAIL SMOKE_PASSWORD; do
  eval "value=\${$var:-}"
  [ -n "$value" ] || missing="$missing $var"
done
if [ -n "$missing" ]; then
  if [ "${SMOKE_ALLOW_SKIP:-0}" = "1" ]; then
    echo "smoke: SMOKE_ALLOW_SKIP=1 -- running WITHOUT the authenticated checks."
    echo "       This proves nothing about auth or the answer path."
  else
    echo "smoke: FAILED before starting -- missing:$missing" >&2
    echo "       These are GitHub environment secrets on the 'production'" >&2
    echo "       environment, passed to deploy.sh on stdin. Set" >&2
    echo "       SMOKE_ALLOW_SKIP=1 only for a deliberate local run." >&2
    exit 1
  fi
fi

BASE="${SMOKE_BASE_URL:-https://localhost}"
CURL=(curl --fail-with-body -sS --max-time 20)
# -k only for the default localhost base, where Caddy serves its own
# locally-trusted certificate. Against a real hostname the certificate
# must validate, and NOT passing -k is what proves it does.
case "$BASE" in
  https://localhost*) CURL+=(-k) ;;
esac

pass=0
fail=0
ok()  { echo "  PASS  $1"; pass=$((pass+1)); }
bad() { echo "  FAIL  $1"; fail=$((fail+1)); }

echo "smoke: $BASE"

# ---- 1. liveness -----------------------------------------------------
if body=$("${CURL[@]}" "$BASE/health"); then
  case "$body" in
    *'"status":"ok"'*) ok "/health is ok" ;;
    *)                 bad "/health said: $body" ;;
  esac
else
  bad "/health did not answer"
fi

# ---- 2. readiness: the database is actually reachable ----------------
# The distinction that matters. /health reports ok with Postgres down,
# by design; only this endpoint round-trips a statement.
if body=$("${CURL[@]}" "$BASE/health/ready"); then
  case "$body" in
    *'"status":"ready"'*) ok "/health/ready is ready" ;;
    *)                    bad "/health/ready said: $body" ;;
  esac
else
  bad "/health/ready is not ready -- the database is unreachable"
fi

# ---- 3. the API docs are NOT published in production -----------------
code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$BASE/openapi.json" || true)
if [ "$code" = "404" ]; then
  ok "/openapi.json is 404 in production"
else
  bad "/openapi.json returned $code"
fi

# ---- 4. an unauthenticated read is refused ---------------------------
code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$BASE/api/v1/documents" || true)
case "$code" in
  401|403) ok "unauthenticated /documents is $code" ;;
  *)       bad "unauthenticated /documents returned $code" ;;
esac

# ---- 5. the smoke account can sign in --------------------------------
# A DEDICATED account, created once by hand (see the runbook). It is not
# created here: a smoke test that registers leaves a new organization on
# every deploy, and "registers nothing permanent" is the requirement.
#
# Reaching this branch with the variables unset is only possible under
# SMOKE_ALLOW_SKIP=1 -- the guard at the top of this script exits
# non-zero otherwise. The wording says what was NOT proven, because
# "SKIP" next to five PASS lines reads like a clean run.
if [ -z "${SMOKE_ORG:-}" ] || [ -z "${SMOKE_EMAIL:-}" ] || [ -z "${SMOKE_PASSWORD:-}" ]; then
  echo "  SKIP  login + answer path NOT TESTED (SMOKE_ALLOW_SKIP=1)"
else
  # The credentials ride in a POST body over TLS and are never echoed.
  # jq is not assumed present on the box, so the token is extracted with
  # sed; the password never reaches a command line or a log either way.
  login_body=$(printf '{"org_name":"%s","email":"%s","password":"%s"}' \
                 "$SMOKE_ORG" "$SMOKE_EMAIL" "$SMOKE_PASSWORD")

  token=$("${CURL[@]}" -X POST "$BASE/api/v1/auth/login" \
            -H 'Content-Type: application/json' \
            -d "$login_body" \
          | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p') || true

  if [ -n "${token:-}" ]; then
    ok "the smoke account signed in"

    # ---- 6. the free question path, end to end ---------------------
    #
    # The route is POST /conversations/{id}/messages with a `content`
    # field. There is NO /conversations/ask endpoint and never was --
    # this block asked for one, so check 6 returned 404 and reported
    # "no citations event", which deploy.sh reads as a failed smoke and
    # ROLLS BACK. Every CD deploy would have rolled back. Never caught
    # because the script had only ever run with SMOKE_* unset, where the
    # whole block SKIPs.
    #
    # An existing conversation is reused, and one created only on the
    # very first run: the stated requirement is that a smoke test
    # registers nothing permanent, and a fresh conversation row per
    # deploy is exactly that. The two Message rows per run are the
    # database write the check exists to exercise.
    convo=$("${CURL[@]}" "$BASE/api/v1/conversations" \
              -H "Authorization: Bearer $token" \
            | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p') || true

    if [ -z "${convo:-}" ]; then
      convo=$("${CURL[@]}" -X POST "$BASE/api/v1/conversations" \
                -H "Authorization: Bearer $token" \
                -H 'Content-Type: application/json' \
                -d '{"title":"smoke checks"}' \
              | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p') || true
    fi

    if [ -z "${convo:-}" ]; then
      bad "could not open a conversation for the assist path"
    else
      # A greeting: intercepted before any provider call. Proves auth, the
      # limiter, SSE framing and a database write for zero quota.
      stream=$("${CURL[@]}" -N -X POST "$BASE/api/v1/conversations/$convo/messages" \
                 -H "Authorization: Bearer $token" \
                 -H 'Content-Type: application/json' \
                 -d '{"content":"hello"}' || true)
      case "$stream" in
        *"event: citations"*) ok "the assist path streamed a citations event (no provider cost)" ;;
        *)                    bad "no citations event on the assist path" ;;
      esac
    fi
  else
    bad "the smoke account could not sign in"
  fi
fi

echo
echo "smoke: $pass passed, $fail failed"
[ "$fail" -eq 0 ]

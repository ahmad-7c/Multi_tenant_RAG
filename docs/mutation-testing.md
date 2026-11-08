# Mutation testing: do the tests actually bite?

A green suite proves the tests pass. It does not prove they would fail
if the code were wrong — and a test that cannot fail is worse than no
test, because it is counted as coverage.

So: break the code on purpose, one change at a time, and check that
something goes red. `scripts/mutate.py` applies one named mutation;
each is a plausible mistake a reviewer might wave through, not
vandalism.

    git checkout -b mutation-testing
    venv/Scripts/python.exe scripts/mutate.py limiter-drops-org
    venv/Scripts/python.exe -m pytest -q tests/test_rate_limit.py
    git checkout -- app eval          # restore before the next one

One at a time, always. Two simultaneous mutations can mask each other:
the first makes a test fail for its own reason and the second goes
unexamined.

## Results — 2026-10-31, commit e82906c, 708 tests

| mutation | what it breaks | caught by |
|---|---|---|
| `limiter-drops-org` | one global bucket for every tenant | `test_two_orgs_do_not_share_a_bucket`, `test_one_orgs_exhausted_bucket_does_not_block_another` |
| `limiter-not-atomic` | read-modify-write instead of the Lua script | `test_the_bucket_is_atomic_under_concurrency` |
| `skip-the-gate` | `should_answer` always returns True | 3 reranker + 2 route tests |
| `usage-drops-org` | the usage query loses its tenant filter | `test_daily_totals_are_scoped_to_one_org` + 2 endpoint tests |
| `ignore-retry-after` | the 429 ships without the header | `test_a_rate_limited_login_returns_429_with_retry_after` |
| `leak-slot-on-disconnect` | slot released after the loop, not in a `finally` | `test_closing_the_body_iterator_still_releases_the_slot` (**see below**) |
| `usage-not-admin-only` | `require_admin` → `get_current_user` | `test_the_usage_endpoint_is_admin_only` |
| `metering-raises` | a failed usage insert propagates | `test_a_failed_insert_returns_none_instead_of_raising` |
| `recall-zero-not-none` | unlabelled cases score 0.0 instead of None | `test_an_unanswerable_case_has_no_recall_rather_than_zero` |
| `keep-thought-parts` | the `thought` filter is removed | 4 of 6 thought-part tests |
| `judge-cache-ignores-answer` | the cache key drops the answer | `test_the_cache_key_changes_with_the_answer_not_just_the_question`, `test_a_changed_answer_is_judged_again` |
| `snapshot-drops-version` | the run file loses the golden-set version | `test_the_snapshot_records_the_golden_set_version_and_counts` |

**12 applied, 12 caught, 0 survivors** — after one real gap was found
and closed.

## Results — 2026-11-04, Phase 5 deploy work, 936 tests

Mutations 21-37, each applied alone and reverted. 17 applied, **17
caught, 0 survivors**. Mutations 21-31 were written with the D1
deploy work but no sweep had ever been recorded for them; they are
swept here for the first time, alongside the six added while fixing
the review findings below.

| mutation | what it breaks | caught by |
|---|---|---|
| `startup-gate-never-fires` | `collect_production_problems` returns `[]` | `test_placeholder_jwt_secrets_are_refused` (+5 more) |
| `startup-gate-skips-production` | the gate treats production as dev | `test_the_secret_value_is_never_echoed_in_the_error`, `test_non_production_is_untouched` |
| `startup-allows-passthrough-reranker` | passthrough reaches production, opening the confidence gate | `test_passthrough_reranker_is_refused` |
| `startup-allows-hosted-reranker-without-key` | hosted reranker with no key | `test_hosted_reranker_without_a_key_is_refused` |
| `startup-checks-gemini-key-for-embeddings-only` | the LLM's key requirement is dropped | `test_a_missing_gemini_key_is_refused_for_both_uses` |
| `startup-accepts-an-empty-gemini-key` | `_absent` becomes `is None` | `test_hosted_reranker_without_a_key_is_refused[""]` |
| `startup-placeholder-prefix-rule-dropped` | only exact placeholders are caught | `test_placeholder_jwt_secrets_are_refused[change-me-something-else-entirely]` |
| `startup-error-echoes-the-secret` | the failed-boot log prints the key | `test_the_secret_value_is_never_echoed_in_the_error` |
| `startup-refuses-one-disabled-budget` | `and` becomes `or`, so one budget is not enough | `test_either_budget_alone_is_enough` |
| `docs-exposed-in-production` | `/docs` and `/openapi.json` come back | `test_the_app_disables_docs_in_production` |
| `gate-not-wired-into-lifespan` | the gate exists and is never called | `test_the_lifespan_actually_calls_the_gate` |
| `readiness-never-queries` | `/health/ready` answers "ready" without asking the database | `test_ready_is_503_when_the_database_is_unreachable` |
| `healthcheck-targets-health-not-ready` | the container probe goes back to `/health` | `test_the_container_healthcheck_targets_readiness_not_health` |
| `edge-trusts-forwarded-for` | `trusted_proxies` returns, making a forged `X-Forwarded-For` trusted | `test_no_trusted_proxies_at_the_edge` |
| `edge-advertises-its-server` | the `Server` header is no longer stripped | `test_the_server_header_is_removed` |
| `prod-example-falls-back-into-gitignore` | `.env.production.example` is swallowed by `.env.*` | `test_the_example_exists_and_is_tracked` |
| `cors-gate-ignores-loopback` | a loopback CORS origin passes the production gate | `test_loopback_origins_are_refused` |

### Two of these were real defects before they were mutations

`healthcheck-targets-health-not-ready` and
`prod-example-falls-back-into-gitignore` are not hypotheticals: both
were the state of the tree. The healthcheck polled `/health`, which
reports `ok` with Postgres unreachable (measured: `/health` 200 and
`POST /auth/login` 500 in the same process), so a deploy with a broken
database would have reported success and P7's automatic rollback would
never have fired. And `.env.production.example` was matched by the
blanket `.env.*` rule, so the file existed only on the author's
machine — `test_the_example_exists_and_is_tracked` asserted
`is_file()`, which is true locally and says nothing about a clone.

`cors-gate-ignores-loopback` is the inverse: the check did not exist
at all, so there was nothing to mutate until it was written.

### One mutation is caught by a test for a different check

`startup-accepts-an-empty-gemini-key` swaps `_absent` for an `is None`
comparison, and the test that fails is
`test_hosted_reranker_without_a_key_is_refused[""]` — a RERANKER test.
`_absent` is shared by both call sites, so either one pins it. Noted
rather than fixed: the helper is correct to be shared, but a reader
tracing coverage for the Gemini key would not find it here.

## The gap this exercise found

`leak-slot-on-disconnect` moves the SSE slot release out of its
`finally` and onto the line after the loop. The first sweep reported it
CAUGHT, and the catch was a false comfort: the failing test was
`test_the_slot_is_released_when_persistence_fails`, while
`test_the_slot_is_released_when_the_client_disconnects` — the test
written specifically for this — **passed with the mutation in place.**

Why: `TestClient` buffers the entire response. Reading one chunk and
abandoning the iterator never cancels anything server-side, because the
generator had already run to completion. The test exercised "stop
reading", which is not "disconnect".

What a real ASGI server does when a client vanishes is call `aclose()`
on the response's body iterator, raising `GeneratorExit` at the
suspended `yield`. A `finally` runs; a statement after the loop does
not. `test_closing_the_body_iterator_still_releases_the_slot` now drives
the route's own `body_iterator` directly: take one chunk, assert the
slot is genuinely held, close the iterator, assert it was released. It
was confirmed failing against the mutation before being committed.

The general lesson, worth more than the fix: **a test named after a
failure mode is not evidence that it exercises that failure mode.** Only
breaking the code tells you.

## Not covered by this exercise

- Mutations inside the Lua scripts themselves (they are strings; a
  mutation there is a Redis-side behaviour change and the harness only
  edits Python).
- `eval/calibrate.py`, which has no unit tests at all — it is driven by
  live runs, and a mutation in it would survive. Its outputs are
  reviewed by hand against the run file instead.
- Anything in the ingestion worker; this sweep targeted Phase 4's new
  surface.

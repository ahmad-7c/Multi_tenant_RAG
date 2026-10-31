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

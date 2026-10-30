---
name: documind-phase4-evaluation
description: Phase 4 playbook for DocuMind: golden set, eval harness, confidence-gate calibration, rate limiting, usage logging. Audit first, score retrieval and generation separately, stop at checkpoints.
---

# DocuMind Phase 4: Evaluation, Calibration, Protection

## Purpose
Prove the RAG system works with numbers, calibrate the confidence gate from data, and protect the system from abuse and runaway cost. "It seems to work" is not a result.

## Operating rules (apply to every step)
1. Teach while building. Before each step state: the approach, one alternative you are NOT taking, and the failure mode the design prevents. After each step give a summary of at most 6 lines.
2. Audit first. Inventory what already exists (Step 0) and build only what is missing, partial or broken. Never rebuild working code.
3. Never print, log or commit API keys. Read them only through Settings. Test output, eval run files and reports must not contain keys.
4. Offline by default. Unit tests use fake providers and never touch the network. Live eval runs are explicit, default to a `--smoke` subset, throttle requests, retry on 429, and report API calls used. Ask before any full live run.
5. Change one variable at a time between eval runs. Every run stores a config snapshot: chunk sizes, model names, thresholds, prompt version, golden set version, git commit.
6. A crash is never a pass. If a check cannot run, report UNKNOWN, never CLEAN or PASS.
7. Decisions belong to the user. Thresholds, failure policies and anything that changes product behavior are proposed with data and tradeoffs, and the user chooses. Defaults marked "pre-approved" below may be applied without asking.
8. Commit per step with a meaningful message. Update "Current Phase" in CLAUDE.md after each step.
9. Stop at each CHECKPOINT and wait.
10. Every report ends with two lists: "Not verified" and "Weakest points".

## Step 0: Audit (always first)
Produce a table: item / status (done, partial, missing, broken) / evidence (file, commit or test name). Items: golden set and loader validation; corpus (PDFs generated from source); eval harness; retrieval metrics; judge; refusal metrics; run storage with config snapshot; threshold calibration; end-to-end refusal measurement; rate limiter; budget guard; usage_events and endpoint; tests for each. Run the test suite and show counts. Then work only on missing, partial or broken items.

## Part 1: Golden set
Location: `eval/golden_set.json`, versioned in git, with a version field.

- Corpus is synthetic or public only. Never real client data. PDFs are generated from source by script, not committed as binaries. At least two corpus documents are multi-page PDFs (4+ pages so header and footer detection is exercised) containing a table, a code block and a repeated header and footer.
- At least two orgs with different documents, so isolation is testable.
- Every question has: id, org, type, question, expected answer or "must refuse", expected source document and page, optional history for follow-ups, and negation-aware `must_mention` / `must_not_assert` checks (never a bare forbidden-substring test).
- Types: `fact`, `exact_term`, `table`, `code`, `spans_two_chunks`, `synthesis`, `follow_up`, `unanswerable_offtopic`, `unanswerable_near_miss`, `cross_tenant_probe`, `ambiguous`.
- Near-miss unanswerables (adjacent topic, clean omission) must outnumber off-topic unanswerables at least 2 to 1. They are the cases that fool a confidence gate.
- A label must have one defensible answer. If it is arguable, use type `ambiguous` with a required explanation field. Ambiguous cases are excluded from gate precision and recall and reported separately. The ambiguous bucket is never a place to park failures.
- Isolation cases come in pairs: an answerable question for org A, and the same question asked by org B, which must retrieve nothing from A. A refusal with no twin proves nothing.
- Every real failure found later becomes a new golden case.
- Target at least 40 questions before treating any threshold as more than provisional. Below that, label every result "provisional, n=N".
- The loader rejects: duplicate ids, missing fields, unpaired isolation cases, unknown types, near-miss to off-topic ratio below 2, ambiguous without explanation, expected source not present in the corpus. Each rule has a test using the broken input it rejects.
- Print counts per type and per org on every run.

## Part 2: Harness
Score stages separately. Never judge only the final answer.

- Retrieval: Recall at k (is an expected source chunk in the top k), MRR, citation page correctness. Computed from retrieved chunk ids, not from answer text.
- Generation: faithfulness (every claim supported by the supplied context), answer relevance, `must_mention` and `must_not_assert` checks, correct refusal on unanswerable questions.
- End to end: correct-refusal rate including questions that pass the gate.
- Judge protocol: LLM-as-judge at temperature 0, claim by claim, strict JSON output, retry on invalid JSON, cache judge results by content hash so reruns do not pay twice. The judge model is its own Setting, separate from the answer model. After the first run, hand-check at least 10 judge grades (include passes and failures) and record the agreement rate in `docs/eval-baseline.md`. If agreement is below 80 percent, fix the judge prompt before trusting any number.
- Reporting: per-type table, per-org table, retrieval and generation side by side, failures listed with case id and one-line reason. Show a small-n warning when any bucket has fewer than 8 cases. Never produce a single blended score.
- Runs are stored as `eval/runs/TIMESTAMP.json` with the config snapshot. `--smoke` runs about 8 representative questions.
- Isolation and cache checks: ask each answerable question twice, the second must be a cache hit with identical text. If the harness itself errors or retrieves nothing because of a crash, the result is UNKNOWN.

## Part 3: Calibration
Goal: choose confidence-gate thresholds from data.

- Record the top reranker score for every gate-scored question. Print the full sorted score lists for should-answer and should-refuse (id, type, score) before suggesting anything.
- State the key fact: a cross-encoder scores relevance, not sufficiency. Near-miss cases can outscore answerable ones, so no threshold alone can fix that.
- Propose reranker threshold A and a vector-similarity floor B (used only when the reranker is unavailable). Pre-approved rule: set each at the midpoint of the largest score gap below the lowest answerable score; if no gap of at least 0.03 exists, use 0.50 for A. Report precision, recall, false answers and false refusals at the chosen values, and also at the value that favours fewer false answers.
- Thresholds belong to a specific reranker model. Store the model name beside the values. Recalibrate if the reranker changes.
- Mark everything provisional with n.
- Then run the end-to-end refusal measurement for ALL should-refuse cases through the full pipeline. If the LLM already refuses the near-misses, no further mechanism is needed. If it fails any, propose options (stronger prompt, structured sufficiency verdict in the same LLM call, separate sufficiency call) with the latency and quota cost of each, and wait for the user. Never build a sufficiency check unasked.
- Apply thresholds as env-overridable Settings. Record data, values, date and n in `docs/eval-baseline.md`.
- When the reranker circuit breaker is open the gate must not silently open fully. Use B and show the degraded state in `/health`.

## Part 4: Rate limiting
- Redis token bucket, atomic (Lua script or equivalent), keyed per org. Stricter limit on the answer endpoint, looser elsewhere. A per-IP limit on login and register.
- 429 with Retry-After and a clear body that names no internal details.
- Cap concurrent SSE streams per org. Release the slot on completion, on error and on client disconnect.
- Global daily budget guard on provider quota with a per-org share, so one tenant cannot consume the whole budget. When exceeded return 503 with a clear message.
- Redis unavailable (pre-approved default): fail open for auth and read endpoints; fail closed for the answer endpoint using a small per-process in-memory fallback bucket. Log every fallback activation.
- Teach: token bucket vs fixed window vs sliding window, why the check must be atomic, why per-org keys matter in a multi-tenant system.

## Part 5: Usage logging
- Table `usage_events`: org_id, user_id, endpoint, embedding tokens, LLM input and output tokens, rerank calls, cache hit, latency ms, status, created_at. Index on (org_id, created_at).
- Written without slowing a request. A logging failure must never fail the request.
- `GET /api/v1/usage`: admin only, org-scoped, daily totals for the last 30 days. Never exposes other orgs.
- Teach: metering is the base for quotas, cost attribution and billing.

## Part 6: Tests (offline, fake providers)
Loader rules; metric functions on a tiny fixed fixture with hand-computed expected values; limiter atomic under concurrency; limits per org do not leak; 429 carries Retry-After; fail-open and fail-closed behave as decided; budget guard trips; SSE slot released on disconnect; usage rows are org-scoped; usage endpoint admin-only; gate refuses below threshold and skips the LLM; degraded mode uses the floor B.

## Part 7: Prove the tests bite
On a throwaway branch apply ONE mutation at a time, confirm a test fails, then restore with `git checkout`. Mutations: remove the org key from the limiter; make the limiter non-atomic; skip the gate; drop org_id from the usage query; ignore Retry-After; leak the SSE slot on disconnect. Any surviving mutation gets a new test, shown failing first. Delete the branch and confirm a clean tree.

## Part 8: Subagents
Delegate noisy or independent work; keep decisions in the main thread.

- `eval-runner`: read-only on code, runs the eval, returns a compact table only.
- `eval-reviewer`: independent read-only skeptic that audits labels, judge reliability, metric code and test strength.

When delegating, give the goal, exact inputs, output format and size limit, and what the subagent must not do. Require evidence (case ids, file and line, test names). Spot-check at least one claim from a subagent report on a critical point before relying on it. Never run two subagents that edit the same files in parallel.

## Checkpoints
1. After Step 0 and Part 1: audit table and golden set counts per type and org. Wait.
2. After Part 3: sorted score lists, proposed A and B, end-to-end refusal results, judge agreement. Wait for the user's threshold decision.
3. After Parts 4 to 7: file tree, limiter code, test counts, mutation results, weakest points, what breaks first at 10x load. Stop.

## Definition of done
Golden set of at least 40 with loader tests; harness reports retrieval and generation separately with per-type tables; judge agreement recorded; thresholds applied with data and marked provisional where n is small; end-to-end refusal measured; limiter, budget guard and usage logging tested; mutations caught; baseline written to `docs/eval-baseline.md`; CLAUDE.md updated; final report includes "Not verified" and "Weakest points".

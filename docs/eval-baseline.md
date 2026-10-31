# Gate calibration baseline

**Provisional, n=38.** Measured 2026-10-31 against live Gemini
(`gemini-embedding-001`, `gemini-3.5-flash-lite`) and live Cohere
(`rerank-v3.5`) over `eval/golden` at golden-set **version 2**.

Raw run: [`eval/runs/20261031T053310Z-full.json`](../eval/runs/20261031T053310Z-full.json)
— every score, every ranked list and the full config snapshot. Reproduce
with:

    venv/Scripts/python.exe -m eval.calibrate

40 questions, **38 gate-scored** (21 should-answer, 17 should-refuse).
The two `ambiguous` cases are excluded from every classification number
here and reported on their own. The run was clean: **0 reranker
fallbacks, 0 rate limits, 0 case errors** — so every score below is a
real cross-encoder relevance score, not an RRF score standing in for
one.

The previous baseline (n=24) is archived at
[`docs/eval-baseline-n24.md`](./eval-baseline-n24.md).

## Applied values

| setting | value | previous (n=24) |
|---|---|---|
| `CONFIDENCE_THRESHOLD` | **0.3230** | 0.4162 |
| `VECTOR_SIMILARITY_FLOOR` | **0.5594** | 0.6175 |

Both belong to **`rerank-v3.5`** and **`gemini-embedding-001` at 1536
dimensions**. Neither transfers to another reranker or embedder; the
scores are not calibrated probabilities and a different cross-encoder
puts them on a different scale. Both are env-overridable.

---

## Retrieval, measured separately from generation

New this run: `recall@k` and MRR, computed from the ranked chunk list
rather than from answer text (`eval/metrics.py`). k is `RERANK_TOP_N`
= 8, the number of parents the prompt actually receives.

| bucket | n | scored | recall@8 | MRR | cited page |
|---|---|---|---|---|---|
| **overall** | 40 | 33 | **1.000** | **0.985** | 0.875 (n=16) |
| answerable_direct | 5 | 5 | 1.000 | 1.000 | 1.000 |
| answerable_needs_rerank | 1 | 1 | 1.000 | 1.000 | — |
| exact_term | 3 | 3 | 1.000 | 1.000 | 1.000 |
| table | 4 | 4 | 1.000 | 0.875 | 1.000 |
| code | 2 | 2 | 1.000 | 1.000 | 0.500 |
| spans_two_chunks | 3 | 3 | 1.000 | 1.000 | 0.667 |
| synthesis | 2 | 2 | 1.000 | 1.000 | 1.000 |
| follow_up | 2 | 1 | 1.000 | 1.000 | 1.000 |
| unanswerable_near | 10 | 10 | 1.000 | 1.000 | — |
| cross_org_isolation | 4 | 0 | — | — | — |
| unanswerable_off_topic | 2 | 0 | — | — | — |
| acme | 35 | 30 | 1.000 | 0.983 | 0.875 |
| globex | 5 | 3 | 1.000 | 1.000 | — |

Read this carefully, because it is easy to over-read:

**Retrieval is not the bottleneck.** Every case with labelled support
found it inside the top 8, and MRR 0.985 means it was almost always at
rank 1. Nothing was demoted by the reranker and nothing was missed. The
`cross_org_isolation` and `unanswerable_off_topic` rows are blank
because those cases label no support — `None`, not `0.0`, since a
question with nothing to retrieve is not a retrieval failure.

**But recall@8 = 1.000 over a 6-document, 40-child corpus is a weak
claim.** With 40 children per tenant, the top 8 is a fifth of the
corpus; recall@8 on a real tenant with 50,000 chunks is a different
measurement entirely. This number says the pipeline is wired correctly
and the chunking does not destroy the support. It does not say retrieval
scales.

**Citation pages: 14 of 16.** Both shortfalls turn out to be
generation findings rather than citation findings, which is what
measuring retrieval separately was for:

- `acme-rollback-cluster-flag` cited **no pages at all**, because the
  gate blocked it and the user got a refusal. Nothing was mis-cited.
- `acme-vsr-tier3-extra-evidence` expects pages **2 and 3** and cited
  only page 3. It is a `spans_two_chunks` case: the answer came from
  one side of the span, so only one page was cited. The citation is
  correct for what the answer says and incomplete for what the question
  asked.

---

## Generation: 3 of 21 answerable cases failed their content check

Retrieval found the support in all three. These are not retrieval
failures and they are the most actionable thing in this document.

**Important: the generated answers below were produced under
`CONFIDENCE_THRESHOLD = 0.4162`**, the value in force during the run
and recorded in the run file's config snapshot. The newly applied
0.3230 changes what the gate admits, so the generation column is not a
prediction of behaviour under the current configuration — this is
precisely why the snapshot records the thresholds a run executed under.

### 1. `acme-rollback-cluster-flag` — a FALSE REFUSAL by the gate

    rerank 0.3395  (the lowest should-answer score in the set)
    answer: "I could not find anything in your organization's documents
             that answers this question."

0.3395 < 0.4162, so the gate blocked a question the corpus answers. The
model never saw the context. **Under the applied 0.3230 this case now
passes the gate** — and whether the model then answers it correctly is
untested, because no run has been made at the new value. It is the case
to watch on the next run, and it is the case the 0.0165 margin is
protecting.

### 2 & 3. Two of three `spans_two_chunks` cases answered HALF the span

This is the real finding. The chunker test
(`test_spans_two_chunks_cases_really_span_two_chunks`) guarantees these
questions need one fact from each side of a child boundary, so only the
parent sent to the LLM can contain the whole answer.

`acme-vsr-questionnaire-to-decision` (rubric wanted `five business
days` and `decision`):

> Once the questionnaire comes back, the security team reviews it within
> five business days of receipt [1]. If the questionnaire shows a gap,
> the vendor submits a written remediation plan with owners and dates
> before the review continues [1]. Finally, the security team verifies
> any remediation the plan calls complete [1].

Fluent, fully grounded, and it stops one step short: it never reaches
the decision. The question asked what happens from questionnaire *to
decision*, and the answer narrates the middle.

`acme-vsr-tier3-extra-evidence` (rubric wanted `penetration test` and
`tier 1`):

> For Tier 3 Restricted engagements, the vendor must also supply a
> penetration test report no more than twelve months old [1].

Correct about tier 3, silent on what tier 1 requires — the
comparison that makes it an "extra" evidence question, and the half
that lives on page 2.

**Both failures look like good answers.** Neither is wrong; both are
incomplete in a way no substring check on the asserted content would
catch, which is why these cases carry a `must_mention` rubric rather
than `expect_phrases`. Parent-child chunking put the whole answer in
the context and the model used the part nearest the retrieved child.

n=3 for this bucket, so "2 of 3 failed" is a count and not a rate. It
is the strongest signal in the run that the next thing to test is
**answer completeness over a parent**, not retrieval.

---

## The selection rule, and where it stopped being enough

The rule is fixed in `eval/thresholds.py` and tested in
`tests/test_threshold_selection.py`: take every score at or below the
lowest should-answer score, find the **widest** gap between consecutive
values, and use its midpoint; fall back to 0.50 if no gap reaches 0.03.

At n=24 exactly one gap qualified, so "widest" did no work. **At n=38,
three gaps qualified, and all three hold recall at 1.000:**

| gap | width | midpoint | TP | FP | TN | FN | precision | recall |
|---|---|---|---|---|---|---|---|---|
| 0.0816 → 0.1832 | 0.1015 | 0.1324 | 21 | 11 | 6 | 0 | 0.656 | 1.000 |
| 0.2241 → 0.3065 | 0.0823 | 0.2653 | 21 | 6 | 11 | 0 | 0.778 | 1.000 |
| 0.3065 → 0.3395 | 0.0330 | **0.3230** | 21 | **4** | 13 | 0 | **0.840** | 1.000 |

The rule as written returns **0.1324**. That is the widest gap, and it
buys the largest margin (0.207 of headroom before a real answer is
lost) — but recall is 1.000 at all three, so the tie-break was spending
precision on margin without being asked to. 0.1324 lets 11 of 17
refusable questions through, including seven near-misses, which are the
cases the gate exists for.

**The operator chose 0.3230**, the highest qualifying gap: same recall,
4 false positives instead of 11, and a margin of 0.0165 below
`acme-rollback-cluster-flag` (0.3395).

The cost of that choice, stated plainly: **0.0165 is a thin margin.**
A reranker-side scoring change, or one new answerable case scoring
below 0.3230, loses a real answer. This is the trade that was made
knowingly, and it is the first thing to re-check on the next run.
`select_threshold` now reports every qualifying gap with its confusion
matrix so the choice is visible rather than implicit.

### For comparison, the full curve

| threshold | TP | FP | TN | FN | precision | recall | answers lost |
|---|---|---|---|---|---|---|---|
| 0.1324 (rule) | 21 | 11 | 6 | 0 | 0.656 | 1.000 | — |
| 0.2653 | 21 | 6 | 11 | 0 | 0.778 | 1.000 | — |
| **0.3230 (applied)** | 21 | 4 | 13 | 0 | 0.840 | 1.000 | — |
| 0.4162 (old value) | 20 | 4 | 13 | 1 | 0.833 | 0.952 | rollback-cluster-flag |
| 0.5533 | 19 | 3 | 14 | 2 | 0.864 | 0.905 | + vsr-questionnaire-to-decision |
| 0.6722 | 14 | 1 | 16 | 7 | 0.933 | 0.667 | + 5 more |

The **fewer-false-answers** option the playbook requires reporting is
0.5533: precision 0.864, recall 0.905. It buys one fewer false answer
by refusing two real ones. That is the right trade only if a wrong
answer costs much more than a missed one — and in this system it does
not, because the prompt refuses most of what the gate lets through (see
below).

Note that the old value **0.4162 is strictly worse than 0.3230** on
this data: same 4 false positives, one fewer true positive.

---

## The central finding, unchanged and now stronger

**The classes overlap and no threshold separates them.** Worst
answerable 0.3395 sits far below best refusable 0.8730.

    should ANSWER  (n=21)   min 0.3395   median 0.8059   max 0.9351
    should REFUSE  (n=17)   min 0.0235   median 0.1952   max 0.8730

Four should-refuse cases score at or above the lowest answerable score
and are therefore **irreducible** — no value of
`CONFIDENCE_THRESHOLD` excludes them without losing a real answer:

| case | score | why it scores high |
|---|---|---|
| `acme-rollback-migrations-how` | 0.8730 | maximally relevant to rollbacks; the corpus just does not say HOW |
| `acme-deploy-friday` | 0.6722 | the deploy-window passage is exactly on topic |
| `acme-vsr-appeal` | 0.6573 | the vendor-review section is the right section |
| `acme-perdiem-weekend` | 0.5258 | the per-diem table is the right table |

A cross-encoder scores **relevance**, not **sufficiency**. These four
questions are maximally relevant to passages that do not answer them.
That is a property of the signal, not of the threshold, and it is why
the gate is described as a cheap filter and never as a correctness
guarantee.

---

## What actually refuses the hard cases: the prompt

End-to-end refusal at 0.3230: **15 of 17 (88%)**. With the gate forced
fully open, the model declined **the same 15** unaided. The gate and the
prompt are not additive here — the prompt is doing the work, and the
gate's contribution is to make the 4 cheapest refusals free.

The two failures are the two that have failed from the start:

- **`acme-rollback-migrations-how`** (0.8730). Answered: *"A rollback
  does not revert database migrations, and reverting a migration is a
  separate, manual change [1]."* Every clause is supported; the judge
  says SUPPORTED. It is arguably a correct response to an
  unanswerable question — it states what the corpus does say and stops.
  Reasonable people disagree about whether this is a failure.
- **`acme-deploy-friday`** (0.6722). Answered: *"No, production deploys
  run Tuesday through Thursday, 09:00 to 16:00 UTC [1]."* The judge says
  SUPPORTED. On the stricter reading this is an inference: "deploys run
  Tue–Thu" does not state what happens on Friday.

**No sufficiency check has been built**, and none should be without
asking: the measured gain is at most 2 of 17 cases, both disputed, and
the cost is either a second model call per question or a structured
verdict inside the answering call that changes the prompt every answer
depends on.

---

## Isolation and cache

**Cross-tenant isolation: 4/4 clean, both directions.** All four probes
retrieved only their own tenant's documents and none answered.
`acme-asks-contractor-expiry` is the direction that was never measured
before n=38 — both original probes ran globex → acme.

**Cache: 20/21 hits, 21/21 byte-identical.** The one miss,
`acme-rollback-cluster-flag`, is correct behaviour and not a cache bug:
that case was refused (it is also the one case whose answer failed its
phrase check, missing `prod-eu`), and `should_cache()` declines to cache
a refusal. A cached refusal would make a transient failure permanent for
the TTL.

---

## Judge reliability: UNMEASURED

`eval/golden/judge_grades.yaml` does not exist, so the agreement rate is
**unknown** — not "good", not "bad". The harness prints
`agreement UNMEASURED` and refuses to conclude.

Every verdict in the end-to-end section above is therefore an
unvalidated judgement by `gemini-3.5-flash-lite`, including the two
SUPPORTED verdicts on the disputed cases. Those two were read by hand
and the answers are quoted in full above precisely because the judge
cannot yet be taken on trust.

**What is needed:** at least 10 hand grades covering both DECLINED and
asserting outcomes. Below 10, `Agreement.meets_bar` returns `None`
rather than `False`.

Note also that `JUDGE_MODEL` currently equals `LLM_MODEL`, so the judge
is grading its own output. It has its own setting for exactly this
reason; pointing it at a stronger model is a config change, not a code
change.

---

## Not verified

- **Retrieval at scale.** recall@8 = 1.000 over 40 children per tenant.
  A real tenant has thousands, and the planner's choice of the HNSW path
  changes with tenant share (see CLAUDE.md's measured finding).
- **Judge agreement.** No hand grades exist.
- **These thresholds under any other provider.** Both are tied to
  `rerank-v3.5` and `gemini-embedding-001`.
- **The degraded (reranker-down) regime end to end.** The cosine floor
  was calibrated from scores collected while the reranker was *up*; no
  case was answered with the breaker actually open.
- **Behaviour under the newly applied 0.3230.** Every generated answer
  in this document was produced at 0.4162. `acme-rollback-cluster-flag`
  (0.3395) was gate-blocked then and is admitted now, and nothing has
  measured what the model does with it.
- **Whether the `spans_two_chunks` failures are a prompt problem or a
  chunking one.** The parent contained the whole answer in both cases;
  the model used half. Not diagnosed.

## Weakest points

1. **The 0.0165 margin at 0.3230.** One answerable case sits just above
   the threshold. This is the chosen trade and the first thing to
   re-examine on the next run.
2. **n=38 is still small.** Several per-type buckets are n=2 or n=3 and
   carry a small-n warning in the report. No per-type number here
   should be read as a rate.
3. **Answer completeness over a parent is unproven.** Two of three
   `spans_two_chunks` cases answered from one side of the span while
   the parent sent to the model contained both. Parent-child chunking
   is the architecture's central retrieval decision, and this is the
   first evidence that the model does not always use what it is given.
4. **The gate is not what produces correct refusals.** 15 of 17 come
   from the prompt. A prompt edit can therefore undo most of the refusal
   behaviour without changing any threshold — which is why
   `eval/runs.py` records the prompt's content hash.
5. **Labels are one person's judgement.** `should_answer` is
   hand-written (deliberately — an LLM writing them grades the system
   against itself), so a systematic misreading of the corpus would move
   every number here in the same direction.
6. **`VECTOR_SIMILARITY_FLOOR = 0.5594` barely filters anything**
   (precision 0.568, 16 false positives). That is honest rather than
   broken: cosine separates these classes very poorly (worst answerable
   0.6868 vs best refusable 0.7437), so in the degraded regime the floor
   blocks only the single most obviously unrelated question. Its job is
   to stop a dead reranker answering *everything*, not to classify well.

# Gate calibration baseline

**Provisional, n=24.** Measured 2026-10-28 against live Gemini
(`gemini-embedding-001`, `gemini-3.5-flash-lite`) and live Cohere
(`rerank-v3.5`) over `eval/golden`. Reproduce with:

    venv/Scripts/python.exe -m eval.calibrate --pace 0 \
        --rerank-timeout 25 --rerank-interval 6.5

24 gate-scored cases (12 should-answer, 12 should-refuse). Two
`ambiguous` cases are excluded from every number here. The run was
clean: 0 reranker fallbacks, 0 rate limits, 0 case errors.

## The selection rule

Fixed in advance, implemented in `eval/thresholds.py`, tested in
`tests/test_threshold_selection.py`:

> Take every score at or below the lowest should-answer score. Find the
> largest gap between consecutive values. The threshold is that gap's
> midpoint. If no gap is at least 0.03 wide, use 0.50.

Searching only *below* the lowest answerable score means the rule
protects **recall** by construction: it cannot cost a real answer on the
measured set. Best-F1 would trade one away for a refusal, because F1
prices those two failures identically and for a grounded-answers product
they are not.

## Chosen values

| setting | value | rule output |
|---|---|---|
| `CONFIDENCE_THRESHOLD` | **0.4162** | midpoint of 0.3065 to 0.5258 (width 0.2194) |
| `VECTOR_SIMILARITY_FLOOR` | **0.6175** | midpoint of 0.5966 to 0.6384 (width 0.0417) |

Both are env-overridable Settings. Neither transfers to another
provider: a Jina or Voyage score of 0.4162 means something else, and a
local cross-encoder logit is not on the same scale at all.

- At `CONFIDENCE_THRESHOLD = 0.4162`: TP 12, FP 3, TN 9, FN 0 =
  precision 0.80, recall 1.00.
- At `VECTOR_SIMILARITY_FLOOR = 0.6175`: TP 12, FP 8, TN 4, FN 0 =
  precision 0.60, recall 1.00.

The floor separates the classes *worse*, which is why it is consulted
only when the reranker is unavailable.

## Reranker score, every case

### should ANSWER (n=12)
| score | case | type |
|---|---|---|
| 0.9181 | acme-followup-above-that | follow_up |
| 0.8968 | acme-prod-deploy-approvals | answerable_needs_rerank |
| 0.8486 | acme-rollback-command | answerable_direct |
| 0.8396 | acme-expense-window | answerable_direct |
| 0.8290 | acme-mfa-production | answerable_direct |
| 0.8128 | globex-parental-leave | answerable_direct |
| 0.8074 | acme-perdiem-international | answerable_needs_rerank |
| 0.7865 | acme-receipt-threshold | answerable_direct |
| 0.6693 | acme-password-rotation | answerable_needs_rerank |
| 0.6111 | acme-approval-7000 | synthesis |
| 0.5889 | acme-password-length | answerable_direct |
| 0.5530 | acme-business-class-6h | synthesis |

### should REFUSE (n=12)
| score | case | type |
|---|---|---|
| 0.8730 | acme-rollback-migrations-how | unanswerable_near |
| 0.6722 | acme-deploy-friday | unanswerable_near |
| 0.5258 | acme-perdiem-weekend | unanswerable_near |
| 0.3065 | acme-mileage-motorcycle | unanswerable_near |
| 0.3065 | acme-followup-after-refusal | follow_up |
| 0.2241 | acme-staging-secrets | unanswerable_near |
| 0.2202 | acme-oncall-weekend-rota | unanswerable_near |
| 0.1927 | acme-mileage-exact-rate | unanswerable_near |
| 0.0486 | acme-vpn-setup | unanswerable_off_topic |
| 0.0257 | globex-asks-expense-window | cross_org_isolation |
| 0.0235 | acme-parental-leave | unanswerable_off_topic |
| 0.0195 | globex-asks-password-length | cross_org_isolation |

## Vector cosine, every case

### should ANSWER (n=12)
| score | case |
|---|---|
| 0.8094 | acme-followup-above-that |
| 0.7927 | acme-approval-7000 |
| 0.7806 | acme-expense-window |
| 0.7775 | acme-rollback-command |
| 0.7749 | acme-perdiem-international |
| 0.7674 | acme-prod-deploy-approvals |
| 0.7499 | acme-mfa-production |
| 0.7254 | acme-password-rotation |
| 0.7069 | acme-password-length |
| 0.6992 | globex-parental-leave |
| 0.6938 | acme-receipt-threshold |
| 0.6868 | acme-business-class-6h |

### should REFUSE (n=12)
| score | case |
|---|---|
| 0.7437 | acme-deploy-friday |
| 0.7325 | acme-perdiem-weekend |
| 0.7255 | acme-staging-secrets |
| 0.7113 | acme-rollback-migrations-how |
| 0.6631 | acme-mileage-motorcycle |
| 0.6631 | acme-followup-after-refusal |
| 0.6547 | acme-oncall-weekend-rota |
| 0.6384 | acme-mileage-exact-rate |
| 0.5966 | acme-vpn-setup |
| 0.5865 | globex-asks-expense-window |
| 0.5531 | globex-asks-password-length |
| 0.5387 | acme-parental-leave |

## The finding that outranks both numbers

**The classes overlap. No threshold separates them.**

- reranker: worst answerable 0.5530 below best refusable 0.8730 (-0.3199)
- cosine: worst answerable 0.6868 below best refusable 0.7437 (-0.0570)

Two should-refuse questions score above *every* answerable case:

| case | score | why it scores high |
|---|---|---|
| `acme-rollback-migrations-how` | 0.8730 | the runbook says a rollback does not revert migrations and that doing so is "a separate, manual change", then never says how |
| `acme-deploy-friday` | 0.6722 | the deploy window is Tue-Thu; Friday is never mentioned |

A cross-encoder scores **relevance**, and these chunks are maximally
relevant to a question they do not answer. That is a property of the
signal, not of the number, so `select_threshold` reports them as
*irreducible* rather than averaging them into a precision figure that
would make the gate look merely mis-tuned. Four cases are irreducible
against the cosine floor for the same reason.

The gate is therefore a **cheap filter, not a correctness guarantee**:
it saves a generation on the easy two thirds. What declines the hard
cases is the grounding instruction in the prompt.

## End-to-end refusal

All 12 should-refuse cases through the full pipeline, and each one again
with the gate **forced open** (`CONFIDENCE_THRESHOLD` lowered for the
call), which is the only way to tell whether the gate or the model is
carrying the refusal.

- **9 blocked by the gate**, which returns the canned refusal.
- **3 passed the gate** (score >= 0.4162): `acme-perdiem-weekend`,
  `acme-deploy-friday`, `acme-rollback-migrations-how`.
- **Model alone, gate forced open: 11 of 12 declined.**

The two irreducible cases, verbatim:

> `acme-rollback-migrations-how`
>
> "A rollback does not revert database migrations, and reverting a
> migration is a separate, manual change [1]. **The provided documents
> do not contain instructions** on how to perform that manual change."

A correct refusal, unaided by the gate.

> `acme-deploy-friday`
>
> "**No**, production deploys run Tuesday through Thursday, 09:00 to
> 16:00 UTC [1]."

Disputed. The judge calls this SUPPORTED, reasoning that a stated window
answers the question. This document calls it an unsupported inference,
because the corpus never says Friday is forbidden or whether exceptions
exist.

End-to-end correct refusal is therefore **11/12 (92%)** on the stricter
reading and 12/12 on the judge's. The disagreement is about whether a
stated window constitutes a stated prohibition, which is the same kind
of scope question that put `acme-lost-phone` and `acme-mfa-contractors`
in the `ambiguous` bucket. `acme-deploy-friday` is a candidate to join
them.

### A note on the judge

`eval/judge.py` originally asked for one verdict and got **both**
decisive cases wrong, grading `rollback-migrations-how` as SUPPORTED:
true of every claim in it, and beside the point, because the answer also
declined. "Is this grounded?" and "did this decline?" are independent
questions, and a judge asked for a single label answers whichever is
easier. It now reports `asserts_answer` and `supported` separately and
derives the label from the pair. After the fix it agrees on
`rollback-migrations-how` and still disagrees on `deploy-friday`.

## Limits of this baseline

- **n=24.** One case moving changes precision by roughly 7 points.
- One corpus, two tenants, one provider pair. Nothing here generalises
  to another reranker or another document set.
- `acme-deploy-friday`'s label is disputed, as above.
- The cosine floor is calibrated on *real* embeddings and is therefore
  meaningless against `FakeEmbeddings`, which is why
  `tests/test_conversations.py` disables it explicitly for that fixture.

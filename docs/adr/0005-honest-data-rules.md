# ADR 0005 — The UI never shows a number the backend did not produce

Status: accepted (B1 through U6, 2026-11-01)

## Context

This product's claim is that an answer can be checked. A trace chip
reading "confidence 0.82" or a relevance bar at 60% is a factual claim
about what the system measured. If any of those can be produced by the
UI in the absence of a measurement, the whole claim is worthless — and
the failure is invisible, because an invented number looks exactly like
a real one.

Three places the UI could invent a figure:

1. A field the backend sent as `null` rendered as `0` or `—`.
2. A number that exists but is **not the kind of number it appears to
   be** — specifically a rank-reciprocal fused score drawn in a 0..1
   relevance bar.
3. A derived statistic (a delta, a rate) computed from an inadequate
   denominator.

## Decision

**Absent is null, never zero.** `candidate_count` and `parent_count`
changed from `int = 0` to `int | None = None` so a cache hit, which
retrieved nothing, reports nothing. The same reasoning as the nullable
token columns on `usage_events`: unknown and zero are different facts.

**The honesty rule is structural, not remembered.** `TraceChip` takes
`value: string` — required. There is no way to pass it "missing" and
have it render a dash, so a caller must omit the chip. Omission is the
only option the type system offers.

**One predicate decides whether a score is relevance.**
`scores_are_relevance(reranker, used_fallback)` is
`scores_are_calibrated && !used_fallback`, and the confidence gate
branches on the same call, so the two cannot drift. Both conditions
matter: `scores_are_calibrated` is a property of the PROVIDER, so a
hosted cross-encoder advertises calibrated scores and still returns
fused order when its call fails — the provider flag alone is true at
exactly the moment the scores are rank reciprocals.

**The score is nulled twice**, where the `Citation` is built and again
where the payload is serialised. The second catches a score replayed
from cache, written under a reranker configuration that can no longer
be verified. Because the two are redundant, **each has its own test** —
otherwise breaking one is invisible and the defence is one layer deep
without anyone knowing which layer.

**Derived statistics decline to exist** rather than mislead:

- A delta is `null` without a complete prior period, so no "+0%"
  claims a comparison nobody made.
- A rate is `null` when the denominator is zero, rendered as an em
  dash.
- A sparkline renders nothing below two points — a one-point trend is
  not a trend — and the card says so.

**Where a measurement is real but reads wrongly, it is suppressed with
a reason.** A gated question with zero candidates genuinely measures
`rerank_ms: 0`; shown as a chip it reads as an impossibly fast rerank
rather than as "there was nothing to rerank", so the chip is omitted
when `candidate_count` is 0.

**Illustrative content is labelled in place.** The hero demo card on
the marketing pages carries the word "Illustration" inside the card.
Its timings are script values, not measurements.

## Consequences

- The UI is quieter than a dashboard that fills every slot. A cache
  hit shows "from cache" and no timings at all, which looks sparse and
  is correct.
- A reader cannot tell "the system did not measure this" from "the UI
  chose not to show it" — both are absence. Accepted: the alternative
  is a placeholder that reads as data.
- `scores_calibrated` must be recomputed anywhere a new ranking path is
  added. The single predicate is what keeps that to one edit.

## Verified

- `tests/test_trace_payload.py` (28 tests) covers null scores under
  passthrough and under fallback, null timings and counts on a cache
  hit, `scores_calibrated` false when nothing was ranked, and the
  OpenAPI contract matching the emitted keys.
- Backend mutations `b1-expose-score-under-fallback`,
  `b1-validate-always-exposes-scores`, `b1-payload-ignores-score-gate`:
  all **CAUGHT**.
- End-to-end test 12 drives a real browser with an uncalibrated
  payload and asserts no confidence chip and no relevance meter, while
  the passage still renders — it is text, not a statistic.
- Frontend mutation `u9-score-when-uncalibrated` (draw the bar anyway,
  defaulting to 0.5): **CAUGHT**.

## Known gap

`scores_calibrated` is `true` on a live answer whose reranker is
calibrated, which is correct — but nothing verifies that the hosted
provider's scores really are calibrated in the 0..1 sense it claims.
That is a provider assertion the system takes on trust.

# Phase 4 evaluation

## Why this exists
`CONFIDENCE_THRESHOLD` is currently a guess. `should_answer` compares a
reranker score against it, and nothing in the repo establishes what a
good or bad score actually looks like — so the gate that is supposed to
turn "nothing relevant here" into an honest refusal is tuned by vibes.

Calibrating it needs labelled question/should-answer pairs. That is what
`golden/` is.

## Layout
    golden/corpus/<org>/*.md   per-tenant documents
    golden/questions.yaml      hand-labelled questions
    loader.py                  parse + validate (validation is the point)

Two orgs on purpose: `acme` has the policies, `globex` has an unrelated
handbook. That is what makes cross-tenant isolation measurable rather
than assumed.

## Running it
    venv/Scripts/python.exe -m eval.loader      # summary + validation
    venv/Scripts/python.exe -m pytest tests/test_golden_set.py

## Categories
| category | what it measures |
|---|---|
| `answerable_direct` | baseline retrieval + grounded answer |
| `answerable_needs_rerank` | the right chunk competes with near-identical ones |
| `answerable_multi_fact` | two statements must be combined |
| `unanswerable_near` | **the gate's real job** — strong retrieval, no answer |
| `unanswerable_off_topic` | the easy gate case, as a control |
| `cross_org_isolation` | a leak here is a security finding, not a quality one |
| `follow_up` | the query rewriter, including refusal exclusion |

`unanswerable_near` is the class that matters. An off-topic question is
refused by any threshold at all, so a set weighted towards off-topic
cases would certify a gate that does not work. A test enforces that the
near cases are not outnumbered.

## Labelling rules
`should_answer: true` means a careful human, given only that org's
corpus, could answer and point at the supporting sentence. It does *not*
mean an LLM would produce plausible text — that is true of every
question here, which is the whole reason the gate exists.

Labels are hand-written, never LLM-generated. "Is this answerable from
the corpus?" is precisely the judgment the gate is graded on, so a model
writing the labels grades the system against itself.

`expect_phrases` must actually occur in the cited documents; the loader
enforces it, because a phrase that appears nowhere makes a case
permanently unpassable and reads as a model failure rather than a
labelling bug.

## Known soft labels
Two cases are deliberately arguable, and are the first thing to review:

- `acme-lost-phone` — the four-hour reporting rule is written about
  laptops ("lost or stolen devices" inside the device-encryption
  section). Whether that covers a phone is unstated. Labelled
  unanswerable; defensible either way.
- `acme-mfa-contractors` — "MFA is mandatory for all staff" is one word
  from answering it, and whether a contractor counts as staff is exactly
  what the corpus omits.

If either label flips, the gate's measured precision moves, so they are
called out rather than buried.

# Phase 4 evaluation

## Why this exists
`CONFIDENCE_THRESHOLD` is a guess. `should_answer` compares a reranker
score against it, and nothing in the repo established what a good or bad
score looks like — so the gate that turns "nothing relevant here" into
an honest refusal was tuned by vibes.

## Layout
    golden/corpus/<org>/      per-tenant documents (3 generated PDFs + 3 md)
    golden/questions.yaml     hand-labelled questions, with a `version`
    golden/judge_grades.yaml  HAND grades the judge is scored against
    build_corpus.py           regenerates the PDFs from source
    loader.py                 parse + validate (validation is the point)
    scoring.py                phrase / rubric / citation-page checks
    metrics.py                recall@k, MRR, citation-page correctness
    judge.py                  the LLM judge: two axes, retries, cache
    thresholds.py             the fixed threshold-selection rule
    runs.py                   run files + the config snapshot
    runs/                     one committed JSON file per run
    calibrate.py              the live run and the threshold curves

## Retrieval is scored separately from generation
`metrics.py` computes **recall@k** (k = `RERANK_TOP_N`, the number of
parents actually sent to the model), **MRR**, and **citation-page
correctness**, from the ranked chunk list and nothing else -- never from
the answer text.

The harness keeps TWO ranked lists per case, which is what makes a
failure diagnosable:

| candidate recall | reranked recall | what broke | where to look |
|---|---|---|---|
| 1.0 | 1.0 | nothing | - |
| 1.0 | 0.0 | the cross-encoder demoted the support | reranker, `RERANK_TOP_N` |
| 0.0 | 0.0 | neither leg found it | embeddings, chunking, the query |

The old single `retrieval_hit` bool could not tell rank 1 from rank 47,
so a change that moved every right answer from rank 2 to rank 30 moved
no number at all.

Relevance is labelled as `(document, page)`, not as a chunk id, because
chunk ids are regenerated on every ingest and by every change to
`CHILD_TOKENS` -- a set pinned to them would go stale *silently*, reading
as a retrieval collapse. The cost is stated in `metrics.py`: on a page
that chunks into several children, any child of that page counts.

## The judge is measured, not trusted
`JUDGE_MODEL` is its own setting. A judge that is the answering model
grades its own output, and the claims it is least likely to flag as
unsupported are exactly the ones it would have inferred itself.

Verdicts are cached on a sha256 of (judge model, grading prompt,
question, context, answer) -- **not** on the case id, since the id is
stable across runs while the answer is not, and an id-keyed cache would
serve a verdict about text the model no longer produces. An
`UNPARSEABLE` reply is retried once with the failure fed back, and is
never cached.

`golden/judge_grades.yaml` holds HAND grades. The report prints the
agreement rate, refuses to conclude below 10 grades (UNKNOWN, not
"unreliable"), and warns when the grades cover only one kind of outcome.

## Runs are files, not one overwritten file
Every run writes `runs/<timestamp>-<label>.json` with a config snapshot:
golden-set version, model names, chunk sizes, thresholds, prompt content
hashes, git commit and dirtiness, and the eval's own flags. See
`runs/README.md`. `--smoke` runs nine representative cases and records
`sufficient_for_calibration: false`.

Two orgs on purpose: `acme` holds the policies, `globex` an unrelated
handbook. That is what makes cross-tenant isolation measurable rather
than assumed.

The PDFs are **build artefacts**, not committed binaries:

    venv/Scripts/python.exe -m eval.build_corpus

A corpus you cannot read in a diff is a corpus nobody will correct, and
"why does the per-diem case expect page 2" is unanswerable once the only
copy of the layout lives inside a PDF. Each is 4 pages — three is the
minimum for `CLEAN_MIN_PAGES_FOR_HEADER_DETECTION`, so a shorter PDF
would not exercise header stripping at all. Between them they carry a
table, a fenced code block with a line continuation, and running
headers/footers that `cleaning.py` must remove.

## Running it
    python -m eval.build_corpus                 # regenerate the PDFs
    python -m eval.loader                       # validate + counts
    python -m pytest tests/test_golden_set.py tests/test_eval_scoring.py \
                     tests/test_eval_metrics.py tests/test_eval_judge.py \
                     tests/test_eval_runs.py
    python -m eval.calibrate --smoke            # 9 cases, harness check
    python -m eval.calibrate --no-generate      # scores and curves only
    python -m eval.calibrate                    # + answers, cache, leaks

The harness itself can be exercised with no network and no spend:

    EMBEDDING_PROVIDER=fake RERANKER_PROVIDER=fake LLM_PROVIDER=fake \
      python -m eval.calibrate --smoke --no-generate --pace 0

Every number that comes out of that is meaningless -- fake embeddings
are not a retrieval system -- and the run file records
`EMBEDDING_PROVIDER: fake` so it cannot later be mistaken for data. What
it proves is that the pipeline, the metrics and the run writer all
execute.

`calibrate.py` needs a live configuration (`EMBEDDING_PROVIDER=gemini`,
`RERANKER_PROVIDER=hosted`) and warns loudly if it does not have one.

### Pacing and the eval's reranker timeout
Two deliberate differences from production:

- `--pace 7` spaces the cases. A Cohere **trial** key allows 10 rerank
  requests per minute; 26 cases back-to-back mostly 429, fall back to
  RRF, and silently poison the curve with the regime it is not
  measuring. Pacing is right here and wrong in the request path — a
  batch job has free wall-clock, a user's query has 3 seconds.
- `--rerank-timeout 25` raises the reranker budget. Production's 3s is a
  *user's* budget, past which RRF order beats waiting. In the eval a
  timeout means the case was scored on RRF instead of the cross-encoder,
  so the run would calibrate the fallback while claiming to calibrate
  the gate. A first pass showed 3s timing out on roughly a third of
  cases. The report now lists any gate-scored case that fell back and
  declares the curve invalid for it.

## Categories
| category | what it measures |
|---|---|
| `answerable_direct` | baseline retrieval + grounded answer |
| `answerable_needs_rerank` | the right chunk competes with near-identical ones |
| `synthesis` | two rules must be combined, or one must NOT be applied |
| `unanswerable_near` | **the gate's real job** — strong retrieval, no answer |
| `unanswerable_off_topic` | the easy gate case, as a control |
| `cross_org_isolation` | a leak here is a security finding, not a quality one |
| `follow_up` | the query rewriter, including refusal exclusion |
| `ambiguous` | **excluded from the gate score**, reported alone |

`unanswerable_near` is the class that matters, and there are seven of
them. An off-topic question is refused by any threshold at all, so a set
weighted towards off-topic cases would certify a gate that does not
work. A test enforces that near cases outnumber off-topic ones at least
two to one.

`ambiguous` is excluded from precision/recall because a case whose
correct answer is arguable cannot discipline a threshold — whichever way
the gate decides, the "error" is in the label. Averaging them in would
launder a judgement call into a number. They are kept, and reported on
their own, because they are exactly the cases a future reader should
re-litigate. Every one must state its `ambiguity` field, so the bucket
cannot become a place to park cases that merely fail.

## Scoring
- `expect_phrases` / `forbid_phrases` — blunt substring checks. Correct
  for leak detection: "thirty days" in a Globex answer is a leak
  whatever the grammar around it, because no sentence in Globex's corpus
  could have produced it.
- `rubric` (`must_mention` / `must_not_assert`) — for synthesis cases,
  where bluntness is wrong. `forbid_phrases: ["cfo"]` fails the
  *correct* answer "finance review is required; CFO approval is not",
  because a substring cannot tell "X is required" from "X is not
  required" — and that distinction is the whole question.
  `must_not_assert` therefore checks whether the term appears in an
  **asserting** context, scoped to its own sentence. It is a heuristic,
  with the limits listed in `scoring.py` and pinned as tests. An LLM
  judge was the alternative: more robust on phrasing, but it puts a
  second model's failure modes and non-determinism inside the
  measurement, and a transparent heuristic that is wrong *visibly* is
  worth more in a number meant to be compared across runs.
- `expect_pages` — a citation with the right document and the wrong page
  is still wrong: it sends a reader to the wrong place in a 4-page
  policy. The loader verifies the support really is on the named page,
  so a mislabelled page cannot score a correct citation as a failure.

## Labelling rules
`should_answer: true` means a careful human, given only that org's
corpus, could answer and point at the supporting sentence. It does *not*
mean an LLM would produce plausible text — that is true of every
question here, which is the whole reason the gate exists.

Labels are hand-written, never LLM-generated. "Is this answerable from
the corpus?" is precisely the judgment the gate is graded on, so a model
writing the labels grades the system against itself.

Unknown fields are a load-time **error**. A typo'd key is the worst kind
of rot: `expect_page: [2]` (singular) parses, validates, silently
disables the page check, and reports a pass the case never earned.

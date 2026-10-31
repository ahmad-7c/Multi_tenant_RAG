# Eval runs

One file per run: `<UTC timestamp>-<label>.json`, written by
`eval/calibrate.py` through `eval/runs.py`.

    {
      "config": { ... what produced these numbers ... },
      "cases":  [ ... one object per question ... ]
    }

**These files are committed.** They are the evidence for the thresholds
in `app/core/config.py` and for every claim in
`docs/eval-baseline.md`. The previous arrangement — a single
`eval/results.json`, holding a bare list, *gitignored* — meant the n=24
run behind `CONFIDENCE_THRESHOLD = 0.4162` left no trace anywhere except
a hand-written markdown file. There was nothing to re-read and nothing
to diff.

## Reading one

`config` answers "what was this measured on":

| key | why it is here |
|---|---|
| `golden_set_version` | a different denominator explains a "regression" that is not one |
| `golden_set_counts` | per type and per org, so a shifted mix is visible |
| `settings` | models, chunk sizes, retrieval shape, the two thresholds |
| `prompts` | content hashes — a prompt edit cannot be forgotten |
| `git` | commit, plus `dirty: true` when the tree was uncommitted |
| `eval_args` | `--pace`, `--rerank-timeout`: these change what a score *means* |
| `selection` | which cases ran, and `sufficient_for_calibration` |
| `judge_cache` / `api_calls` | what the run actually cost |

`EMBEDDING_PROVIDER`, `LLM_PROVIDER` and `RERANKER_PROVIDER` are in
`settings`, so a run made with fake providers — which produces complete,
plausible, meaningless numbers — is identifiable from the file rather
than from whoever remembers running it.

No key, token or connection string is ever written here: the snapshot is
built from an **allow-list** of setting names (`eval/runs.SNAPSHOT_SETTINGS`),
so a secret added to Settings tomorrow cannot appear in a run file by
default.

## Comparing two

    from eval.runs import load_run, diff_configs
    diff_configs(load_run(old)["config"], load_run(new)["config"])

returns one line per difference. That function is the point of the
snapshot: without it a reviewer compares two 400-line files by eye and
attributes a threshold change to the reranker.

## Which runs can set a threshold

Only `label: full`. A `--smoke` run (nine cases) and an `--only` run
exist to check that the harness works; a threshold fitted to them is
fitted to whichever cases happened to be selected.
`config.selection.sufficient_for_calibration` records this in the file,
because a reader who finds it later should not have to infer it from the
case count.

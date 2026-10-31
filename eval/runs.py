"""Storing an eval run so two runs can actually be compared.

## What was wrong before

`calibrate.py` ended with:

    json.dump([asdict(r) for r in results], handle)   # -> eval/results.json

One bare list, one fixed filename. Three consequences, all of which bit:

  * **Each run overwrote the last.** The n=24 run that produced
    `CONFIDENCE_THRESHOLD = 0.4162` was erased by the next run of
    anything, so the evidence for a value sitting in Settings existed
    only in a markdown file written by hand afterwards.
  * **No run said what produced it.** Not the model names, not the
    thresholds in force, not the chunk sizes, not the golden set
    version, not the commit. A file of 40 scores with no configuration
    is not a measurement; it is 40 numbers.
  * **Nothing could be diffed.** "Did recall drop because the reranker
    changed or because I added fourteen questions?" is unanswerable
    without a snapshot, and it is the only question anyone asks of a
    second run.

## The rule this file enforces

A run file is written as `eval/runs/<UTC timestamp>-<label>.json` and
contains two top-level keys: `config` and `cases`. `config` is captured
BEFORE the first case executes, from the live Settings object, so it
describes the run that happened rather than the one intended.

## Why the prompt is recorded as a HASH, not a version number

The alternative was `PROMPT_VERSION = 3` in `prompting.py`, bumped by
hand. That fails in exactly the situation it exists for: someone edits
the grounding rules to fix a refusal, does not think of it as a version
change, and the next run is silently compared against a different
prompt. A sha256 of the prompt text cannot be forgotten -- it changes
when the bytes change, including on a whitespace edit, which is the
correct behaviour for a prompt whose whitespace is inside a delimiter
block the model is told to respect.

Twelve hex characters, not sixty-four: this is a label for a human
diffing two files, not a cryptographic commitment.

## Why the git commit is recorded AND is not enough

The commit pins `app/`, which the hashes and settings already cover
indirectly. It is kept because it pins everything NOT in the snapshot
(the chunker's internals, `retrieval.py`'s SQL, the cleaning rules), and
`dirty: true` is recorded alongside it because a run from an uncommitted
tree is not reproducible from the commit alone, and silently implying
otherwise is worse than admitting it.

No key, token or `.env` value is ever written here. The snapshot records
provider NAMES and model NAMES; `_redacted_settings` is an allow-list,
not a deny-list, so a new secret added to Settings cannot leak into a
run file by default.
"""
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone

EVAL_ROOT = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(EVAL_ROOT, "runs")

# The ONLY settings copied into a run file. An allow-list, so adding a
# secret to Settings cannot leak it here: a new field is absent from the
# snapshot until someone names it, and a missing field is a nuisance
# whereas a leaked key is an incident.
SNAPSHOT_SETTINGS = (
    # providers and models -- names only, never keys
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_MAX_TOKENS",
    "LLM_TEMPERATURE",
    "REWRITE_MODEL",
    "JUDGE_MODEL",
    "RERANKER_PROVIDER",
    "RERANKER_MODEL",
    "RERANKER_TIMEOUT_SECONDS",
    # chunking -- changes these and every chunk id, every page mapping
    # and every recall figure moves
    "PARENT_TOKENS",
    "CHILD_TOKENS",
    "CHILD_OVERLAP",
    # retrieval shape
    "RETRIEVE_CANDIDATES",
    "RETRIEVE_PER_LEG",
    "RRF_K",
    "RERANK_TOP_N",
    "HNSW_EF_SEARCH",
    "HNSW_ITERATIVE_SCAN",
    # the values being calibrated. Recorded even though a calibration
    # run is meant to CHOOSE them: a run made under the old threshold
    # saw the old gate, and the answers it generated reflect that.
    "CONFIDENCE_THRESHOLD",
    "VECTOR_SIMILARITY_FLOOR",
    "CONTEXT_TOKEN_BUDGET",
)

# Words that mark a secret, matched as whole underscore-separated WORDS
# and never as substrings. The substring version of this check was
# written first and silently dropped CHILD_TOKENS, PARENT_TOKENS,
# LLM_MAX_TOKENS and CONTEXT_TOKEN_BUDGET from every snapshot -- "token"
# is inside "tokens", and those four are the settings that decide what a
# recall figure even means. A redaction rule that quietly removes the
# measurement is worse than no rule: the file still looks complete.
_SECRET_WORDS = frozenset({"key", "secret", "password", "credential", "dsn"})


def is_secret_name(name: str) -> bool:
    """Does this setting NAME look like a secret?

    Whole words only. `CHILD_TOKENS` is a count, `RERANKER_API_KEY` is a
    credential, and a substring test cannot tell them apart.

    "token" is handled by SUFFIX rather than as a word, because the two
    senses collide head-on in this codebase: `GITHUB_TOKEN` would be a
    credential while `CONTEXT_TOKEN_BUDGET`, `CHILD_TOKENS` and
    `LLM_MAX_TOKENS` are counts that decide what every retrieval number
    in the file means. A rule that redacts the counts leaves a snapshot
    that looks complete and describes a different corpus cut.

    URLs count as secrets because DATABASE_URL and REDIS_URL carry
    passwords in practice.
    """
    lowered = name.lower()
    words = set(lowered.split("_"))
    if words & _SECRET_WORDS:
        return True
    return lowered.endswith(("url", "_token")) or lowered == "token"


def _redacted_settings(settings) -> dict:
    out = {}
    for name in SNAPSHOT_SETTINGS:
        if is_secret_name(name):
            # Belt and braces: the allow-list above is the real
            # protection. This is what stops a future
            # "RERANKER_BASE_URL" being appended to the list without
            # anyone thinking about it.
            continue
        out[name] = getattr(settings, name, None)
    return out


def prompt_fingerprint() -> dict:
    """Content hashes of the prompts an answer depends on."""
    from app.services import prompting

    def sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    fingerprint = {"system_prompt_sha": sha(prompting.SYSTEM_PROMPT)}
    # Named separately because a change to either one changes what a
    # refusal LOOKS like, and the judge matches the system's own refusal
    # text before spending a model call on it.
    for name in ("INSUFFICIENT_CONTEXT_ANSWER", "NOT_FOUND_ANSWER"):
        text = getattr(prompting, name, None)
        if text is None:
            from app.services import reranker

            text = getattr(reranker, name, None)
        if text is not None:
            fingerprint[f"{name.lower()}_sha"] = sha(text)
    return fingerprint


def git_state() -> dict:
    """Commit and cleanliness, or an explicit unknown.

    A failure here is recorded, never raised: an eval run that has
    already paid for live API calls must not be thrown away because
    `git` is missing from PATH.
    """
    def run(*args):
        return subprocess.run(
            args, cwd=EVAL_ROOT, capture_output=True, text=True, timeout=10
        )

    try:
        commit = run("git", "rev-parse", "HEAD")
        status = run("git", "status", "--porcelain")
        if commit.returncode != 0:
            return {"commit": None, "dirty": None, "error": commit.stderr.strip()[:200]}
        return {
            "commit": commit.stdout.strip(),
            # A dirty tree means the commit does not reproduce this run.
            # Saying so is the whole value of the field.
            "dirty": bool(status.stdout.strip()),
        }
    except Exception as exc:
        return {"commit": None, "dirty": None, "error": f"{type(exc).__name__}: {exc}"}


def config_snapshot(*, golden, settings, args=None, extra=None) -> dict:
    """Everything needed to say what this run measured.

    Called before the first case, deliberately: `calibrate.py` mutates
    `settings.RERANKER_TIMEOUT_SECONDS` for the run, and a snapshot
    taken afterwards would record the eval's 25s budget as though it
    were production's 3s.
    """
    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "golden_set_version": golden.version,
        "golden_set_counts": {
            "questions": len(golden.questions),
            "gate_scored": len(golden.gate_scored()),
            "per_type": _counts(q.category for q in golden.questions),
            "per_org": _counts(q.org for q in golden.questions),
        },
        "settings": _redacted_settings(settings),
        "prompts": prompt_fingerprint(),
        "git": git_state(),
    }
    if args is not None:
        # The eval's own knobs. `--pace` and `--rerank-timeout` change
        # what a score MEANS (a timeout scores the case on RRF), so a
        # run that does not record them cannot be compared to one that
        # used different ones.
        snapshot["eval_args"] = {
            k: v for k, v in vars(args).items() if not is_secret_name(k)
        }
    if extra:
        snapshot.update(extra)
    return snapshot


def _counts(values) -> dict:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def run_filename(*, label: str = "full", when=None) -> str:
    when = when or datetime.now(timezone.utc)
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    # The label reaches the filesystem, and it comes from the command
    # line. Dots are dropped entirely rather than merely stripped at the
    # ends: `--label ..` produced "<stamp>-...json" with the first
    # version of this, which is a valid filename but a confusing one,
    # and `--label ../../x` has no business resolving anywhere.
    safe = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-") or "run"
    return f"{stamp}-{safe}.json"


def write_run(
    *, config: dict, cases, label: str = "full", runs_dir: str = RUNS_DIR
) -> str:
    """Write one run file and return its path.

    A dict with `config` and `cases`, never a bare list: a list has
    nowhere to put the snapshot, which is how the old file ended up
    describing nothing.
    """
    os.makedirs(runs_dir, exist_ok=True)
    path = os.path.join(runs_dir, run_filename(label=label))
    payload = {
        "config": config,
        "cases": [asdict(c) if is_dataclass(c) else c for c in cases],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
    return path


def load_run(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        # The pre-snapshot format. Readable, and explicitly marked as
        # having no configuration rather than quietly given an empty
        # one -- "no config recorded" and "config recorded as default"
        # are different claims.
        return {"config": {"legacy_no_snapshot": True}, "cases": payload}
    return payload


def list_runs(runs_dir: str = RUNS_DIR) -> list[str]:
    if not os.path.isdir(runs_dir):
        return []
    return sorted(
        os.path.join(runs_dir, name)
        for name in os.listdir(runs_dir)
        if name.endswith(".json")
    )


def diff_configs(older: dict, newer: dict) -> list[str]:
    """Human-readable list of what changed between two run configs.

    The point of a snapshot is this function. Without it a reviewer
    compares two 400-line JSON files by eye and misses the one line
    that explains the whole difference in the numbers.
    """
    lines: list[str] = []

    def compare(path, a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                compare(f"{path}.{key}" if path else key, a.get(key), b.get(key))
        elif a != b:
            lines.append(f"{path}: {a!r} -> {b!r}")

    for key in ("golden_set_version", "settings", "prompts", "git", "eval_args"):
        compare(key, older.get(key), newer.get(key))
    return lines

"""Run files must describe the run that produced them.

The failure these tests exist to prevent is not a crash. It is a
directory of plausible-looking JSON files that cannot be compared,
which is what `eval/results.json` was: one fixed filename, a bare list,
no models, no thresholds, no commit, overwritten by the next run.

Two properties matter most and both are pinned below:

  * a snapshot carries NO secrets, enforced by an allow-list rather
    than a blocklist, so a key added to Settings tomorrow cannot leak
    into a run file by default;
  * two runs are diffable, and the diff names the one setting that
    changed -- without that, a reviewer compares two 400-line files by
    eye and attributes a threshold change to the reranker.
"""
import json
from dataclasses import dataclass

import pytest

from app.core.config import settings
from eval.runs import (
    SNAPSHOT_SETTINGS,
    is_secret_name,
    config_snapshot,
    diff_configs,
    git_state,
    list_runs,
    load_run,
    prompt_fingerprint,
    run_filename,
    write_run,
)


@dataclass
class _FakeGolden:
    version: str = "7"

    @property
    def questions(self):
        class Q:
            def __init__(self, category, org):
                self.category = category
                self.org = org

        return [Q("table", "acme"), Q("table", "acme"), Q("follow_up", "globex")]

    def gate_scored(self):
        return self.questions[:2]


@dataclass
class _FakeCase:
    id: str
    score: float


@pytest.fixture()
def golden():
    return _FakeGolden()


# ------------------------------------------------------------ snapshot --


def test_the_snapshot_records_the_golden_set_version_and_counts(golden):
    snapshot = config_snapshot(golden=golden, settings=settings)
    assert snapshot["golden_set_version"] == "7"
    assert snapshot["golden_set_counts"]["questions"] == 3
    assert snapshot["golden_set_counts"]["per_type"] == {"follow_up": 1, "table": 2}
    assert snapshot["golden_set_counts"]["per_org"] == {"acme": 2, "globex": 1}


def test_the_snapshot_records_the_settings_that_change_a_score(golden):
    snapshot = config_snapshot(golden=golden, settings=settings)
    recorded = snapshot["settings"]
    # The ones a reader needs to interpret any number in the file: what
    # model produced it, what the gate was, and how the corpus was cut.
    for name in (
        "EMBEDDING_MODEL",
        "LLM_MODEL",
        "RERANKER_MODEL",
        "JUDGE_MODEL",
        "CONFIDENCE_THRESHOLD",
        "VECTOR_SIMILARITY_FLOOR",
        "CHILD_TOKENS",
        "RERANK_TOP_N",
    ):
        assert name in recorded, name


def test_no_secret_reaches_a_snapshot(golden):
    snapshot = config_snapshot(golden=golden, settings=settings)
    blob = json.dumps(snapshot).lower()
    for forbidden in ("api_key", "apikey", "secret", "password", "jwt_secret"):
        assert forbidden not in blob, forbidden
    # And the real values, in case a name was spelled differently.
    for attribute in (
        "GEMINI_API_KEY",
        "RERANKER_API_KEY",
        "OPENAI_API_KEY",
        "JWT_SECRET_KEY",
        "DATABASE_URL",
    ):
        value = getattr(settings, attribute, None)
        if value:
            assert str(value).lower() not in blob, attribute


def test_the_allow_list_is_an_allow_list():
    # The protection is structural: a new Settings field is absent from
    # a snapshot until someone names it here. A deny-list would have the
    # opposite default, and the default is what matters the day someone
    # adds STRIPE_SECRET_KEY.
    assert "GEMINI_API_KEY" not in SNAPSHOT_SETTINGS
    assert "RERANKER_API_KEY" not in SNAPSHOT_SETTINGS
    for name in SNAPSHOT_SETTINGS:
        # Whole words, matching is_secret_name. A substring rule fails
        # here on CHILD_TOKENS ("token" inside "tokens") -- and it
        # failed in the code the same way, silently dropping every
        # chunking setting out of the snapshot.
        assert not is_secret_name(name), name


def test_the_secret_name_rule_tells_a_count_from_a_credential():
    assert is_secret_name("RERANKER_API_KEY") is True
    assert is_secret_name("JWT_SECRET_KEY") is True
    assert is_secret_name("DATABASE_URL") is True
    # The four that the substring version wrongly removed. These decide
    # what a recall number means, so losing them makes the snapshot
    # look complete while describing a different corpus cut.
    assert is_secret_name("CHILD_TOKENS") is False
    assert is_secret_name("PARENT_TOKENS") is False
    assert is_secret_name("LLM_MAX_TOKENS") is False
    assert is_secret_name("CONTEXT_TOKEN_BUDGET") is False
    # But a real credential ending in _TOKEN still is one. That is the
    # collision the suffix rule exists to resolve.
    assert is_secret_name("GITHUB_TOKEN") is True


def test_the_prompt_is_fingerprinted_by_CONTENT(monkeypatch):
    from app.services import prompting

    before = prompt_fingerprint()["system_prompt_sha"]
    monkeypatch.setattr(
        prompting, "SYSTEM_PROMPT", prompting.SYSTEM_PROMPT + "\nan extra rule"
    )
    after = prompt_fingerprint()["system_prompt_sha"]
    # A hand-maintained PROMPT_VERSION fails exactly here: someone edits
    # the grounding rules, does not think of it as a version bump, and
    # the next run is compared against a different prompt.
    assert before != after


def test_the_fingerprint_is_short_enough_to_read():
    # A label for a human diffing two files, not a commitment.
    assert len(prompt_fingerprint()["system_prompt_sha"]) == 12


def test_the_snapshot_records_the_evals_own_knobs(golden):
    import argparse

    # A Namespace, which is what argparse hands the harness. A plain
    # class would put these on the CLASS and vars() would see nothing.
    args = argparse.Namespace(pace=7.0, rerank_timeout=25.0, smoke=False)
    snapshot = config_snapshot(golden=golden, settings=settings, args=args)
    # --rerank-timeout changes what a score MEANS: a timeout scores the
    # case on RRF, so a run that does not record it cannot be compared
    # with one that used 3s.
    assert snapshot["eval_args"]["rerank_timeout"] == 25.0
    assert snapshot["eval_args"]["pace"] == 7.0


def test_git_state_reports_dirtiness_or_an_explicit_unknown():
    state = git_state()
    assert set(state) >= {"commit", "dirty"}
    if state["commit"] is not None:
        # A dirty tree means the commit does not reproduce the run.
        # Saying so is the whole value of the field.
        assert isinstance(state["dirty"], bool)
    else:
        assert "error" in state


# ----------------------------------------------------------- run files --


def test_a_run_file_is_timestamped_and_labelled():
    from datetime import datetime, timezone

    when = datetime(2026, 10, 31, 9, 5, 1, tzinfo=timezone.utc)
    assert run_filename(label="smoke", when=when) == "20261031T090501Z-smoke.json"
    # A label from the command line must not escape the directory.
    assert "/" not in run_filename(label="../../etc/passwd", when=when)
    assert ".." not in run_filename(label="..", when=when)


def test_a_run_file_carries_config_and_cases_not_a_bare_list(tmp_path, golden):
    snapshot = config_snapshot(golden=golden, settings=settings)
    path = write_run(
        config=snapshot,
        cases=[_FakeCase("a", 0.9), _FakeCase("b", 0.1)],
        label="full",
        runs_dir=str(tmp_path),
    )
    payload = json.loads(open(path, encoding="utf-8").read())
    # A list has nowhere to put the snapshot, which is how the old file
    # ended up describing nothing.
    assert isinstance(payload, dict)
    assert payload["config"]["golden_set_version"] == "7"
    assert [c["id"] for c in payload["cases"]] == ["a", "b"]


def test_two_runs_do_not_overwrite_each_other(tmp_path, golden):
    snapshot = config_snapshot(golden=golden, settings=settings)
    first = write_run(config=snapshot, cases=[], runs_dir=str(tmp_path), label="one")
    second = write_run(config=snapshot, cases=[], runs_dir=str(tmp_path), label="two")
    assert first != second
    assert len(list_runs(str(tmp_path))) == 2


def test_the_old_bare_list_format_still_loads_and_says_it_has_no_config(tmp_path):
    legacy = tmp_path / "results.json"
    legacy.write_text(json.dumps([{"id": "a"}]), encoding="utf-8")
    payload = load_run(str(legacy))
    # "No config recorded" and "config recorded as the default" are
    # different claims, and conflating them would attribute today's
    # settings to a run made under unknown ones.
    assert payload["config"]["legacy_no_snapshot"] is True
    assert payload["cases"] == [{"id": "a"}]


def test_listing_a_missing_runs_directory_is_empty_not_an_error(tmp_path):
    assert list_runs(str(tmp_path / "never-created")) == []


# --------------------------------------------------------------- diff --


def test_the_diff_names_the_one_setting_that_changed(golden):
    older = config_snapshot(golden=golden, settings=settings)
    newer = json.loads(json.dumps(older))
    newer["settings"]["CONFIDENCE_THRESHOLD"] = 0.75

    lines = diff_configs(older, newer)
    assert any("CONFIDENCE_THRESHOLD" in line and "0.75" in line for line in lines)
    # And nothing else, so the signal is not buried.
    assert len(lines) == 1


def test_the_diff_catches_a_changed_golden_set_version(golden):
    older = config_snapshot(golden=golden, settings=settings)
    newer = json.loads(json.dumps(older))
    newer["golden_set_version"] = "8"
    lines = diff_configs(older, newer)
    # The single most important line: a different denominator explains a
    # "regression" that is not one.
    assert any(line.startswith("golden_set_version") for line in lines)


def test_an_identical_config_diffs_to_nothing(golden):
    snapshot = config_snapshot(golden=golden, settings=settings)
    assert diff_configs(snapshot, json.loads(json.dumps(snapshot))) == []

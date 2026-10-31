"""The judge's machinery: shortcuts, retries, caching, agreement.

None of these tests ask whether the judge GRADES well -- that is what
the hand-grade agreement rate is for, and it needs a live model. They
pin the parts that can be wrong silently:

  * a canned refusal must be recognised without paying for a call,
  * an unparseable reply must be retried with a correction rather than
    re-sent verbatim,
  * the cache must key on the ANSWER (not the case id), or a rerun
    serves verdicts about text the model no longer produces,
  * an unparseable verdict must never be cached, or one bad reply
    becomes permanent,
  * the agreement rate must refuse to conclude on too few grades
    instead of reporting a confident number from three samples.
"""
import json

import pytest

from app.services.prompting import INSUFFICIENT_CONTEXT_ANSWER
from eval import judge as judge_module
from eval.judge import (
    DECLINED,
    SUPPORTED,
    UNPARSEABLE,
    UNSUPPORTED,
    JudgeCache,
    cache_key,
    canned_refusal,
    judge_answer,
    load_hand_grades,
    score_agreement,
)

pytestmark = pytest.mark.asyncio


class ScriptedLLM:
    """Returns the next scripted reply and counts calls.

    Records the USER prompt of every call, because the retry behaviour
    being tested is specifically that the second prompt differs from the
    first.
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, *, system, user, max_tokens=None, temperature=None):
        self.prompts.append(user)
        self.temperatures = getattr(self, "temperatures", [])
        self.temperatures.append(temperature)
        if not self.replies:
            raise AssertionError("judge called more times than scripted")
        return self.replies.pop(0)

    async def stream(self, **kwargs):  # pragma: no cover - never used here
        raise AssertionError("the judge must use complete(), not stream()")


def _verdict_json(asserts, supported, reason="because"):
    return json.dumps(
        {"asserts_answer": asserts, "supported": supported, "reason": reason}
    )


@pytest.fixture()
def no_cache():
    return JudgeCache(enabled=False)


@pytest.fixture()
def disk_cache(tmp_path):
    return JudgeCache(directory=str(tmp_path / "judge-cache"), enabled=True)


# ------------------------------------------------------- the shortcut --


async def test_the_systems_own_refusal_costs_no_model_call(no_cache):
    llm = ScriptedLLM()  # scripted with nothing: a call would raise
    verdict = await judge_answer(
        question="q",
        context="c",
        answer=INSUFFICIENT_CONTEXT_ANSWER,
        llm=llm,
        cache=no_cache,
    )
    assert verdict.verdict == DECLINED
    assert verdict.deterministic is True
    assert verdict.model_calls == 0
    assert llm.prompts == []


def test_an_empty_answer_is_not_scored_as_a_refusal():
    # An empty answer is a bug (a hung stream, a dropped frame). Calling
    # it DECLINED would credit the confidence gate for someone else's
    # failure and make the refusal rate look better than it is.
    verdict = canned_refusal("")
    assert verdict.verdict == UNSUPPORTED
    assert verdict.deterministic is True


# ------------------------------------------------------------ retries --


async def test_prose_is_retried_once_with_a_correction(no_cache):
    llm = ScriptedLLM("Sure! Here is my assessment: the answer looks fine.",
                      _verdict_json(True, True))
    verdict = await judge_answer(
        question="q", context="c", answer="a", llm=llm, cache=no_cache, max_retries=1
    )
    assert verdict.verdict == SUPPORTED
    assert verdict.model_calls == 2
    # The second prompt must DIFFER. At temperature 0 a verbatim resend
    # is the same input, and the same input is the same unparseable
    # reply -- a retry that cannot change the answer is a retry that
    # only spends money.
    assert llm.prompts[0] != llm.prompts[1]
    assert "could not be parsed" in llm.prompts[1]


async def test_a_second_failure_gives_up_as_unparseable(no_cache):
    llm = ScriptedLLM("nope", "still nope")
    verdict = await judge_answer(
        question="q", context="c", answer="a", llm=llm, cache=no_cache, max_retries=1
    )
    assert verdict.verdict == UNPARSEABLE
    assert verdict.model_calls == 2
    # The raw reply is kept so the case can be read by hand, which is
    # what the harness tells the operator to do.
    assert verdict.raw == "still nope"


async def test_retries_can_be_disabled(no_cache):
    llm = ScriptedLLM("nope")
    verdict = await judge_answer(
        question="q", context="c", answer="a", llm=llm, cache=no_cache, max_retries=0
    )
    assert verdict.verdict == UNPARSEABLE
    assert verdict.model_calls == 1


async def test_a_fenced_object_parses_without_a_retry(no_cache):
    llm = ScriptedLLM("```json\n" + _verdict_json(False, False) + "\n```")
    verdict = await judge_answer(
        question="q", context="c", answer="a", llm=llm, cache=no_cache
    )
    assert verdict.verdict == DECLINED
    assert verdict.model_calls == 1


async def test_the_judge_grades_at_temperature_zero(no_cache):
    llm = ScriptedLLM(_verdict_json(True, True))
    await judge_answer(question="q", context="c", answer="a", llm=llm, cache=no_cache)
    # Not LLM_TEMPERATURE (0.1). A sampled grade is how one answer gets
    # two labels in two runs, which makes a rerun uncomparable.
    assert llm.temperatures == [0.0]


# -------------------------------------------------------------- cache --


def test_the_cache_key_changes_with_the_answer_not_just_the_question():
    base = dict(question="q", context="c", model="m")
    assert cache_key(answer="a", **base) != cache_key(answer="b", **base)
    # The case ID is deliberately absent from the key. Between runs the
    # id is stable while the answer changes, so an id-keyed cache would
    # serve a verdict about text the model no longer produces.


def test_the_cache_key_changes_with_the_model():
    base = dict(question="q", context="c", answer="a")
    assert cache_key(model="m1", **base) != cache_key(model="m2", **base)


def test_the_cache_key_changes_when_the_grading_rubric_changes(monkeypatch):
    base = dict(question="q", context="c", answer="a", model="m")
    before = cache_key(**base)
    monkeypatch.setattr(judge_module, "SYSTEM", judge_module.SYSTEM + "\nnew rule")
    # Otherwise every stored verdict survives a rubric edit and the run
    # reports the OLD rubric's grades as the new one's.
    assert cache_key(**base) != before


def test_the_cache_key_cannot_be_confused_by_a_different_field_split():
    # ("ab", "c") and ("a", "bc") must not hash the same. Without a
    # separator between fields they do, and one case is served another
    # case's verdict.
    a = cache_key(question="ab", context="c", answer="x", model="m")
    b = cache_key(question="a", context="bc", answer="x", model="m")
    assert a != b


async def test_a_second_identical_judgement_is_served_from_cache(disk_cache):
    llm = ScriptedLLM(_verdict_json(True, False))
    first = await judge_answer(
        question="q", context="c", answer="a", llm=llm, cache=disk_cache
    )
    second = await judge_answer(
        question="q", context="c", answer="a", llm=llm, cache=disk_cache
    )
    assert first.verdict == second.verdict == UNSUPPORTED
    assert second.cached is True
    assert second.model_calls == 0
    # One call total: the scripted LLM would raise on a second.
    assert len(llm.prompts) == 1


async def test_a_changed_answer_is_judged_again(disk_cache):
    llm = ScriptedLLM(_verdict_json(True, True), _verdict_json(False, False))
    await judge_answer(question="q", context="c", answer="a1", llm=llm, cache=disk_cache)
    second = await judge_answer(
        question="q", context="c", answer="a2", llm=llm, cache=disk_cache
    )
    assert second.verdict == DECLINED
    assert second.cached is False
    assert len(llm.prompts) == 2


async def test_an_unparseable_verdict_is_never_cached(disk_cache):
    llm = ScriptedLLM("nope", "nope again", _verdict_json(True, True))
    first = await judge_answer(
        question="q", context="c", answer="a", llm=llm, cache=disk_cache, max_retries=1
    )
    assert first.verdict == UNPARSEABLE
    # Caching it would make one bad reply permanent for this answer, and
    # an UNPARSEABLE is a transport failure, not a judgement.
    second = await judge_answer(
        question="q", context="c", answer="a", llm=llm, cache=disk_cache, max_retries=0
    )
    assert second.verdict == SUPPORTED
    assert second.cached is False


async def test_a_corrupt_cache_file_is_a_miss_not_a_crash(disk_cache, tmp_path):
    llm = ScriptedLLM(_verdict_json(True, True))
    key = cache_key(question="q", context="c", answer="a", model="m")
    import os

    os.makedirs(disk_cache.directory, exist_ok=True)
    with open(os.path.join(disk_cache.directory, f"{key}.json"), "w") as handle:
        handle.write("{ this is not json")

    verdict = await judge_answer(
        question="q", context="c", answer="a", llm=llm, cache=disk_cache, model_name="m"
    )
    # Losing a cached verdict costs one API call. Crashing costs the
    # whole run, including every live call already paid for.
    assert verdict.verdict == SUPPORTED


async def test_a_disabled_cache_never_writes(tmp_path):
    cache = JudgeCache(directory=str(tmp_path / "off"), enabled=False)
    llm = ScriptedLLM(_verdict_json(True, True), _verdict_json(True, True))
    await judge_answer(question="q", context="c", answer="a", llm=llm, cache=cache)
    await judge_answer(question="q", context="c", answer="a", llm=llm, cache=cache)
    assert len(llm.prompts) == 2
    assert not (tmp_path / "off").exists()


# ---------------------------------------------------------- agreement --


def test_agreement_counts_matches_and_lists_disagreements():
    judged = {"a": DECLINED, "b": SUPPORTED, "c": UNSUPPORTED}
    hand = {"a": DECLINED, "b": UNSUPPORTED, "c": UNSUPPORTED}
    result = score_agreement(judged, hand)
    assert (result.n, result.agreed) == (3, 2)
    assert result.rate == pytest.approx(2 / 3)
    assert result.disagreements == [
        {"id": "b", "judge": SUPPORTED, "hand": UNSUPPORTED}
    ]


def test_a_hand_grade_for_a_missing_case_is_reported_not_dropped():
    # This is what a renamed case id looks like. Dropping it silently
    # shrinks the denominator and inflates the rate.
    result = score_agreement({"a": DECLINED}, {"a": DECLINED, "renamed": DECLINED})
    assert result.unmatched_grades == ["renamed"]
    assert result.n == 1


def test_too_few_grades_is_unknown_rather_than_a_failure():
    judged = {f"c{i}": DECLINED for i in range(5)}
    result = score_agreement(judged, {f"c{i}": DECLINED for i in range(5)})
    assert result.rate == 1.0
    # 5 perfect grades is not evidence the judge works, and reporting
    # False would send someone to fix a prompt that may be fine.
    assert result.meets_bar is None


def test_ten_grades_at_eighty_percent_meets_the_bar():
    judged = {f"c{i}": DECLINED for i in range(10)}
    hand = dict(judged)
    hand["c0"] = SUPPORTED
    hand["c1"] = SUPPORTED
    result = score_agreement(judged, hand)
    assert result.n == 10
    assert result.rate == 0.8
    assert result.meets_bar is True


def test_seventy_percent_fails_the_bar():
    judged = {f"c{i}": DECLINED for i in range(10)}
    hand = dict(judged)
    for i in range(3):
        hand[f"c{i}"] = SUPPORTED
    assert score_agreement(judged, hand).meets_bar is False


def test_grades_covering_only_refusals_are_flagged_as_one_sided():
    judged = {f"c{i}": DECLINED for i in range(10)}
    one_sided = score_agreement(judged, dict(judged))
    # Every hand grade is DECLINED, so the agreement rate says nothing
    # about whether the judge can spot an unsupported assertion.
    assert one_sided.covers_both_outcomes is False

    hand = dict(judged)
    hand["c0"] = UNSUPPORTED
    assert score_agreement(judged, hand).covers_both_outcomes is True


# ------------------------------------------------------- hand grades --


def test_missing_hand_grades_are_empty_not_an_error(tmp_path):
    # The first run has nothing to compare against. Refusing to run
    # would mean the judge is never measured at all.
    assert load_hand_grades(str(tmp_path / "nothing.yaml")) == {}


def test_a_typo_in_a_hand_grade_is_rejected(tmp_path):
    path = tmp_path / "judge_grades.yaml"
    path.write_text('grades:\n  case-a: DECLINE\n', encoding="utf-8")
    # "DECLINE" is not a verdict. Loading it would count as the judge
    # disagreeing with a human on every run.
    with pytest.raises(ValueError, match="unknown verdict"):
        load_hand_grades(str(path))


def test_valid_hand_grades_load(tmp_path):
    path = tmp_path / "judge_grades.yaml"
    path.write_text(
        'grades:\n  case-a: DECLINED\n  case-b: UNSUPPORTED\n', encoding="utf-8"
    )
    assert load_hand_grades(str(path)) == {"case-a": DECLINED, "case-b": UNSUPPORTED}

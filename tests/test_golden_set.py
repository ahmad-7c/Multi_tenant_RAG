"""The golden set's own integrity.

The eval measures the system. These tests measure the eval -- because
every way a golden set rots produces a plausible NUMBER rather than an
error. A renamed document silently scores zero recall; a duplicate id
silently drops a case; an expect_phrase that occurs nowhere in the
corpus makes a case permanently unpassable and reads as a model
failure. None of that is visible in a score.

So each validator rule below is tested by feeding it the broken input it
exists to reject, not by trusting that it works.
"""
import textwrap

import pytest

from eval.loader import (
    CATEGORIES,
    GoldenSetError,
    load_golden_set,
    summarise,
)


@pytest.fixture(scope="module")
def golden():
    return load_golden_set()


# ---- the real set ----------------------------------------------------


def test_the_real_golden_set_validates(golden):
    assert golden.questions
    assert golden.documents


def test_every_question_explains_why_it_exists(golden):
    for question in golden.questions:
        # A case nobody can justify is a case nobody will dare delete
        # when it starts failing for the wrong reason.
        assert question.why.strip(), f"{question.id} has an empty 'why'"
        assert len(question.why.strip()) > 40, f"{question.id}: 'why' is too thin"


def test_the_gate_labels_are_balanced(golden):
    """Roughly even should_answer true/false.

    Not cosmetic. The gate will be scored on these labels, and a set
    that is 80% answerable makes "always answer" look like 80%
    accuracy -- which is precisely the behaviour the gate exists to
    prevent.
    """
    answerable = sum(1 for q in golden.questions if q.should_answer)
    refusable = len(golden.questions) - answerable

    assert answerable >= 5 and refusable >= 5
    ratio = answerable / len(golden.questions)
    assert 0.35 <= ratio <= 0.65, f"unbalanced: {answerable} answerable of {len(golden.questions)}"


def test_the_hard_refusals_outnumber_the_easy_ones(golden):
    """Topically-near unanswerables must dominate off-topic ones.

    An off-topic question is refused by any threshold at all, so a set
    weighted towards them would certify a gate that is actually broken.
    The near cases are the ones where retrieval returns a strong chunk
    that still does not answer the question.
    """
    near = len(golden.by_category("unanswerable_near"))
    off_topic = len(golden.by_category("unanswerable_off_topic"))

    assert near >= off_topic, f"{near} near vs {off_topic} off-topic"


def test_isolation_cases_are_pairs_with_opposite_labels(golden):
    """Each isolation question must have an identical twin that IS answered.

    Without the positive twin, "refuse everything for globex" scores a
    perfect isolation result. The pair is what makes a leak
    distinguishable from blanket refusal.
    """
    isolation = golden.by_category("cross_org_isolation")
    assert isolation, "no isolation cases"

    for case in isolation:
        assert case.should_answer is False
        twins = [
            q
            for q in golden.questions
            if q.question == case.question and q.org != case.org
        ]
        assert twins, f"{case.id}: no other org asks this exact question"
        assert any(t.should_answer for t in twins), (
            f"{case.id}: no org ANSWERS this question, so refusing it proves nothing"
        )


def test_isolation_cases_forbid_the_other_orgs_answer(golden):
    for case in golden.by_category("cross_org_isolation"):
        # The leak has to be detectable. Without forbid_phrases a
        # cross-tenant answer looks like any other answer.
        assert case.forbid_phrases, f"{case.id}: no forbid_phrases to detect a leak"


def test_follow_ups_are_meaningless_standalone(golden):
    for case in golden.by_category("follow_up"):
        assert case.history
        # If a follow-up retrieves fine on its own, it is not testing
        # the rewriter.
        assert len(case.question.split()) <= 6, (
            f"{case.id}: too specific to require history"
        )


def test_categories_are_all_exercised(golden):
    used = {q.category for q in golden.questions}
    unused = CATEGORIES - used
    assert not unused, f"declared but unused categories: {sorted(unused)}"


def test_summarise_runs(golden):
    assert "questions" in summarise(golden)


# ---- the validator catches the ways this file rots -------------------

HEADER = "questions:\n"

VALID_ENTRY = """\
  - id: good-one
    org: acme
    question: How long do I have to submit an expense report?
    should_answer: true
    expect_documents: [travel-policy.md]
    expect_phrases: ["thirty days"]
    category: answerable_direct
    why: >
      A long enough justification to pass the thinness check applied to
      every case in the real set.
"""


def _write(tmp_path, body):
    path = tmp_path / "questions.yaml"
    path.write_text(HEADER + textwrap.dedent(body), encoding="utf-8")
    return str(path)


def _load(tmp_path, body):
    return load_golden_set(questions_path=_write(tmp_path, body))


def test_a_valid_entry_loads(tmp_path):
    golden = _load(tmp_path, VALID_ENTRY)
    assert golden.get("good-one").should_answer is True


def test_a_duplicate_id_is_rejected(tmp_path):
    with pytest.raises(GoldenSetError, match="duplicate question id"):
        _load(tmp_path, VALID_ENTRY + VALID_ENTRY)


def test_an_unknown_org_is_rejected(tmp_path):
    with pytest.raises(GoldenSetError, match="has no corpus"):
        _load(tmp_path, VALID_ENTRY.replace("org: acme", "org: initech"))


def test_a_renamed_document_is_rejected(tmp_path):
    # The realistic rot: someone renames a corpus file and every case
    # pointing at it starts scoring zero recall instead of erroring.
    with pytest.raises(GoldenSetError, match="expect_documents references"):
        _load(tmp_path, VALID_ENTRY.replace("travel-policy.md", "travel_policy_v2.md"))


def test_a_phrase_absent_from_the_corpus_is_rejected(tmp_path):
    with pytest.raises(GoldenSetError, match="does not occur"):
        _load(tmp_path, VALID_ENTRY.replace('"thirty days"', '"forty five days"'))


def test_an_answerable_case_without_expected_documents_is_rejected(tmp_path):
    with pytest.raises(GoldenSetError, match="no expect_documents"):
        _load(tmp_path, VALID_ENTRY.replace(
            "    expect_documents: [travel-policy.md]\n", ""))


def test_an_answerable_case_without_expected_phrases_is_rejected(tmp_path):
    with pytest.raises(GoldenSetError, match="no expect_phrases"):
        _load(tmp_path, VALID_ENTRY.replace(
            '    expect_phrases: ["thirty days"]\n', ""))


def test_a_refusal_case_demanding_phrases_is_rejected(tmp_path):
    # "Must refuse" and "must contain these words" cannot both hold.
    body = VALID_ENTRY.replace("should_answer: true", "should_answer: false")
    with pytest.raises(GoldenSetError, match="cannot be required to contain"):
        _load(tmp_path, body)


def test_an_unknown_category_is_rejected(tmp_path):
    with pytest.raises(GoldenSetError, match="unknown category"):
        _load(tmp_path, VALID_ENTRY.replace(
            "category: answerable_direct", "category: vibes"))


def test_a_follow_up_without_history_is_rejected(tmp_path):
    with pytest.raises(GoldenSetError, match="requires history"):
        _load(tmp_path, VALID_ENTRY.replace(
            "category: answerable_direct", "category: follow_up"))


def test_a_non_boolean_should_answer_is_rejected(tmp_path):
    # YAML reads a bare `yes` as True but a quoted "yes" as a string,
    # and a string is truthy -- so this would silently label a refusal
    # case as answerable.
    with pytest.raises(GoldenSetError, match="must be a bool"):
        _load(tmp_path, VALID_ENTRY.replace(
            "should_answer: true", 'should_answer: "yes"'))


def test_a_missing_required_field_is_rejected(tmp_path):
    with pytest.raises(GoldenSetError, match="missing required field 'why'"):
        body = VALID_ENTRY
        body = body[: body.index("    why: >")]
        _load(tmp_path, body)


# ---- the corpus survives the real chunker ---------------------------


def test_the_per_diem_table_stays_in_one_chunk(golden):
    """The table case is only meaningful if the table is not split.

    acme-perdiem-international is the one case that exercises the
    chunker's table handling, and it would quietly become a different
    test -- three unrelated rows in three chunks -- if the table were
    ever split. Asserted against the real chunker, not assumed.
    """
    from app.services.chunking import chunk_pages
    from app.services.extraction import Page

    document = next(
        d for d in golden.documents_for("acme") if d.filename == "travel-policy.md"
    )
    parents = chunk_pages([Page(1, document.content)])

    holding = [p for p in parents if "95 USD" in p.content]
    assert holding, "the international per-diem row vanished from every parent"
    # All three bands in one parent: that is what makes the three rows
    # competing candidates for the reranker rather than separate chunks.
    assert any(
        "64 USD" in p.content and "79 USD" in p.content and "95 USD" in p.content
        for p in parents
    ), "the per-diem table was split across parents"

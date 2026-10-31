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
    GATE_EXCLUDED,
    GATE_SCORED,
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


def test_the_gate_scored_labels_are_balanced(golden):
    """Roughly even should_answer true/false among GATE-SCORED cases.

    Not cosmetic. The gate is scored on these labels, and a population
    that is 80% answerable makes "always answer" look like 80% accuracy
    -- precisely the behaviour the gate exists to prevent.

    Measured over gate_scored() rather than every question, because the
    ambiguous cases are excluded from the score; counting them in the
    balance would balance a population nobody scores.
    """
    scored = golden.gate_scored()
    answerable = sum(1 for q in scored if q.should_answer)
    refusable = len(scored) - answerable

    assert answerable >= 5 and refusable >= 5
    ratio = answerable / len(scored)
    assert 0.35 <= ratio <= 0.65, (
        f"unbalanced: {answerable} answerable of {len(scored)} scored"
    )


def test_ambiguous_cases_are_excluded_from_the_gate_score(golden):
    """The ambiguous bucket must not reach precision/recall.

    A case whose correct answer is arguable cannot discipline a
    threshold: whichever way the gate decides, the "error" is in the
    label. Averaging them in would launder a judgement call into a
    number.
    """
    for case in golden.by_category("ambiguous"):
        assert case.counts_toward_gate is False
    assert "ambiguous" in GATE_EXCLUDED
    assert not (GATE_SCORED & GATE_EXCLUDED)


def test_every_ambiguous_case_states_its_ambiguity(golden):
    # Enforced by the loader; asserted here so the REAL set is checked,
    # not only the synthetic fixtures. Without it, `ambiguous` becomes a
    # place to park cases that merely fail.
    ambiguous = golden.by_category("ambiguous")
    assert ambiguous, "the ambiguous bucket is empty -- did a case get reclassified?"


def test_there_are_enough_near_miss_unanswerables(golden):
    """The near-miss bucket must not be tiny.

    These are the only cases that can distinguish a well-tuned gate from
    a broken one: retrieval returns a STRONG chunk that still does not
    answer. With two or three of them, one coin-flip moves the measured
    precision by a third.
    """
    near = golden.by_category("unanswerable_near")
    assert len(near) >= 6, f"only {len(near)} near-miss cases"


def test_the_hard_refusals_outnumber_the_easy_ones(golden):
    """Topically-near unanswerables must dominate off-topic ones.

    An off-topic question is refused by any threshold at all, so a set
    weighted towards them would certify a gate that is actually broken.
    The near cases are the ones where retrieval returns a strong chunk
    that still does not answer the question.
    """
    near = len(golden.by_category("unanswerable_near"))
    off_topic = len(golden.by_category("unanswerable_off_topic"))

    assert near >= 2 * off_topic, f"{near} near vs {off_topic} off-topic"


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

# A version is now mandatory, so the shared header carries one. The
# three tests that police the rule itself write their own header.
HEADER = 'version: "test"\nquestions:\n'


def _write_raw(tmp_path, text):
    path = tmp_path / "questions.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)

VALID_ENTRY = """\
  - id: good-one
    org: acme
    question: How long do I have to submit an expense report?
    should_answer: true
    expect_documents: [travel-policy.pdf]
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


def test_the_version_is_loaded_and_is_a_string(tmp_path):
    golden = _load(tmp_path, VALID_ENTRY)
    # A string, not an int: YAML reads `version: 2` as an integer and
    # `version: 2.0` as a float that compares equal to it, so two
    # different sets could record the same version.
    assert golden.version == "test"
    assert isinstance(golden.version, str)


def test_a_set_with_no_version_is_rejected(tmp_path):
    # Every run file records the version. Without one, two runs over
    # different sets are indistinguishable, and the change in the
    # denominator reads as a regression in the system.
    path = _write_raw(tmp_path, "questions:\n" + textwrap.dedent(VALID_ENTRY))
    with pytest.raises(GoldenSetError, match="top-level 'version'"):
        load_golden_set(questions_path=path)


def test_a_blank_version_is_rejected(tmp_path):
    path = _write_raw(
        tmp_path,
        'version: "  "\nquestions:\n' + textwrap.dedent(VALID_ENTRY),
    )
    with pytest.raises(GoldenSetError, match="must not be blank"):
        load_golden_set(questions_path=path)


def test_the_real_set_declares_a_clean_version(golden):
    assert golden.version
    assert golden.version.strip() == golden.version


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
        _load(tmp_path, VALID_ENTRY.replace("travel-policy.pdf", "travel_policy_v2.pdf"))


def test_a_phrase_absent_from_the_corpus_is_rejected(tmp_path):
    with pytest.raises(GoldenSetError, match="does not occur"):
        _load(tmp_path, VALID_ENTRY.replace('"thirty days"', '"forty five days"'))


def test_an_answerable_case_without_expected_documents_is_rejected(tmp_path):
    with pytest.raises(GoldenSetError, match="no expect_documents"):
        _load(tmp_path, VALID_ENTRY.replace(
            "    expect_documents: [travel-policy.pdf]\n", ""))


def test_an_answerable_case_without_phrases_or_a_rubric_is_rejected(tmp_path):
    # Either is acceptable now -- expect_phrases for direct cases, a
    # rubric for synthesis -- but neither means "answered" could not be
    # told apart from "answered wrongly".
    with pytest.raises(GoldenSetError, match="neither expect_phrases nor a rubric"):
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


def test_the_corpus_has_real_multi_page_pdfs(golden):
    """At least two PDFs, each with several pages.

    Markdown fixtures cannot exercise PyMuPDF extraction, running
    header/footer stripping, or page numbers reaching a citation -- and
    those are three of the places a citation can be confidently wrong.
    """
    pdfs = [d for d in golden.documents if d.is_pdf]
    assert len(pdfs) >= 2, f"only {len(pdfs)} PDF(s) in the corpus"
    for document in pdfs:
        # 3 is CLEAN_MIN_PAGES_FOR_HEADER_DETECTION: below it the
        # header stripping deliberately does not run, so a 2-page PDF
        # would not exercise it.
        assert len(document.pages) >= 3, (
            f"{document.filename} has {len(document.pages)} pages; "
            f"header/footer detection needs at least 3"
        )


def test_running_headers_and_footers_are_stripped(golden):
    """Page furniture must not survive into retrievable text.

    If it does, every chunk of a 4-page policy carries "Acme Corporation
    - Internal Use Only", which both pollutes the embedding and gives a
    citation snippet that quotes a footer back at the reader.
    """
    furniture = [
        "Internal Use Only",
        "Confidential",
        "Travel and Expense Policy v4.2",
        "Deployment Runbook rev 11",
    ]
    for document in golden.documents:
        if not document.is_pdf:
            continue
        for page in document.pages:
            for phrase in furniture:
                assert phrase not in page.text, (
                    f"{document.filename} p{page.page_number} still carries "
                    f"the running {phrase!r}"
                )


def test_page_numbers_are_one_indexed_and_contiguous(golden):
    for document in golden.documents:
        numbers = [p.page_number for p in document.pages]
        # "page 0" is not something a reader can act on.
        assert numbers == list(range(1, len(numbers) + 1)), (
            f"{document.filename} has page numbers {numbers}"
        )


def test_the_per_diem_table_stays_in_one_chunk(golden):
    """The table case is only meaningful if the table is not split.

    acme-perdiem-international is scored on reaching the International
    row, and it would quietly become a different test -- three unrelated
    rows in three chunks -- if the table were ever split. Asserted
    against the real chunker on the real PDF, not assumed.
    """
    from app.services.chunking import chunk_pages
    from app.services.extraction import Page

    document = golden.document("acme", "travel-policy.pdf")
    parents = chunk_pages([Page(p.page_number, p.text) for p in document.pages])

    holding = [p for p in parents if "95 USD" in p.content]
    assert holding, "the international per-diem row vanished from every parent"
    assert any(
        "64 USD" in p.content and "79 USD" in p.content and "95 USD" in p.content
        for p in parents
    ), "the per-diem table was split across parents"
    # And it must be on the page the label claims.
    assert holding[0].page_number == 2, (
        f"the per-diem table is on page {holding[0].page_number}, "
        f"but acme-perdiem-international expects page 2"
    )


def test_the_rollback_code_fence_survives_intact(golden):
    """A code fence split mid-command is worse than no answer.

    acme-rollback-command scores on "acmectl rollback" reaching the
    user. The command spans a continuation line, so a chunker that split
    inside the fence would still retrieve and would hand over a command
    that does not run.
    """
    from app.services.chunking import chunk_pages
    from app.services.extraction import Page

    document = golden.document("acme", "engineering-runbook.pdf")
    parents = chunk_pages([Page(p.page_number, p.text) for p in document.pages])

    holding = [p for p in parents if "acmectl rollback" in p.content]
    assert holding, "the rollback command vanished"
    parent = holding[0]
    assert parent.page_number == 2
    # Both halves of the command, and the fence that contains them.
    assert "--service api" in parent.content
    assert "--cluster prod-eu" in parent.content
    assert parent.content.count("```") >= 2, "the code fence was left unclosed"

    # The child that carries it must carry all of it, since children are
    # what actually get embedded and searched.
    carriers = [
        c
        for p in parents
        for c in p.children
        if "acmectl rollback" in c.content
    ]
    assert carriers, "no CHILD chunk carries the command, so it cannot be retrieved"
    assert any("--cluster prod-eu" in c.content for c in carriers), (
        "the command was split across children mid-invocation"
    )


def test_expect_pages_point_at_pages_that_contain_the_support(golden):
    """Enforced by the loader; asserted against the REAL set here.

    A label naming the wrong page would score a correct citation as
    wrong, which looks like a retrieval bug and is not one.
    """
    checked = 0
    for question in golden.questions:
        if not question.expect_pages or not question.expect_phrases:
            continue
        for filename in question.expect_documents:
            document = golden.document(question.org, filename)
            for number in question.expect_pages:
                page = document.page(number)
                if page is None:
                    continue
                text = page.text.lower()
                assert any(p in text for p in question.expect_phrases), (
                    f"{question.id}: none of {list(question.expect_phrases)} is on "
                    f"{filename} p{number}"
                )
                checked += 1
    assert checked >= 5, f"only {checked} page/phrase pairs checked"


def test_synthesis_cases_are_scored_by_rubric_not_substring(golden):
    """Synthesis cases must use a rubric.

    A bare forbidden substring cannot tell "the CFO is required" from
    "the CFO is not required", and that distinction IS the question.
    """
    synthesis = golden.by_category("synthesis")
    assert synthesis, "no synthesis cases"
    for case in synthesis:
        assert case.rubric, f"{case.id}: synthesis case without a rubric"
        assert case.rubric.get("must_mention"), f"{case.id}: rubric has no must_mention"
        assert not case.forbid_phrases, (
            f"{case.id}: uses forbid_phrases, which is the blunt check this "
            f"category exists to avoid"
        )


def test_an_unknown_field_is_rejected(tmp_path):
    # The worst golden-set rot: `expect_page` (singular) parses fine,
    # validates fine, and silently disables the page check -- so the
    # case reports a pass it never earned.
    body = VALID_ENTRY + "    expect_page: [1]\n"
    with pytest.raises(GoldenSetError, match="unknown field"):
        _load(tmp_path, body)


def test_an_ambiguous_case_without_an_ambiguity_is_rejected(tmp_path):
    body = VALID_ENTRY.replace("should_answer: true", "should_answer: false")
    body = body.replace('    expect_phrases: ["thirty days"]\n', "")
    body = body.replace("category: answerable_direct", "category: ambiguous")
    with pytest.raises(GoldenSetError, match="must state the 'ambiguity'"):
        _load(tmp_path, body)


# ---- set-level rules, added in the Phase 4 expansion -----------------
#
# These three were previously asserted only against the real set, which
# meant an interactive load_golden_set() -- the eval harness included --
# accepted a set that had rotted. They now live in the loader, so each
# is tested here the same way as the per-entry rules: by feeding it the
# broken input it exists to reject.

ANSWERED_TWIN = """\
  - id: acme-answers-it
    org: acme
    question: What is the minimum password length?
    should_answer: true
    expect_documents: [security-policy.md]
    expect_phrases: ["fourteen"]
    category: answerable_direct
    why: >
      The answerable half of the isolation pair, long enough to pass the
      thinness check applied to every case in the real set.
"""

PROBE = """\
  - id: globex-probes-it
    org: globex
    question: What is the minimum password length?
    should_answer: false
    forbid_phrases: ["fourteen"]
    category: cross_org_isolation
    why: >
      The probe half of the isolation pair, long enough to pass the
      thinness check applied to every case in the real set.
"""


def test_a_paired_isolation_case_loads(tmp_path):
    # The positive control. Without it the three rejection tests below
    # would also pass against a loader that refused every isolation case.
    golden = _load(tmp_path, ANSWERED_TWIN + PROBE)
    assert golden.get("globex-probes-it").should_answer is False
    assert golden.get("acme-answers-it").should_answer is True


def test_an_isolation_probe_without_forbid_phrases_is_rejected(tmp_path):
    body = ANSWERED_TWIN + PROBE.replace('    forbid_phrases: ["fourteen"]\n', "")
    with pytest.raises(GoldenSetError, match="forbid_phrases"):
        _load(tmp_path, body)


def test_an_isolation_probe_with_no_twin_is_rejected(tmp_path):
    # The probe alone. "Refuse everything for globex" would score a
    # perfect isolation result against this set.
    with pytest.raises(GoldenSetError, match="no twin|exact question"):
        _load(tmp_path, PROBE)


def test_an_isolation_probe_whose_twin_also_refuses_is_rejected(tmp_path):
    # A twin exists and asks the identical question, but no org ANSWERS
    # it -- so the pair cannot distinguish a working org filter from a
    # pipeline that refuses this question for everybody.
    refusing_twin = ANSWERED_TWIN.replace(
        "    should_answer: true\n", "    should_answer: false\n"
    ).replace(
        '    expect_phrases: ["fourteen"]\n', ""
    )
    with pytest.raises(GoldenSetError, match="none ANSWERS"):
        _load(tmp_path, refusing_twin + PROBE)


OFF_TOPIC = """\
  - id: acme-way-off
    org: acme
    question: How do I reserve a parking space at the office?
    should_answer: false
    category: unanswerable_off_topic
    why: >
      An off-topic refusal, long enough to pass the thinness check that
      is applied to every case in the real set.
"""

NEAR_MISS = """\
  - id: acme-near-{n}
    org: acme
    question: What is the mileage reimbursement rate for a motorcycle {n}?
    should_answer: false
    expect_documents: [travel-policy.pdf]
    category: unanswerable_near
    why: >
      A near-miss refusal, long enough to pass the thinness check that
      is applied to every case in the real set.
"""


def test_off_topic_refusals_outnumbering_near_misses_are_rejected(tmp_path):
    # One off-topic case and one near miss: 1 < 2 * 1. A set weighted
    # towards off-topic refusals certifies a gate that is broken, since
    # any threshold at all refuses an off-topic question.
    with pytest.raises(GoldenSetError, match="at least twice"):
        _load(tmp_path, OFF_TOPIC + NEAR_MISS.format(n=1))


def test_the_ratio_rule_accepts_twice_as_many_near_misses(tmp_path):
    golden = _load(
        tmp_path, OFF_TOPIC + NEAR_MISS.format(n=1) + NEAR_MISS.format(n=2)
    )
    assert len(golden.questions) == 3


def test_the_ratio_rule_does_not_fire_without_off_topic_cases(tmp_path):
    # A small set with no off-topic bucket is not malformed, it is just
    # small. Firing here would make every synthetic fixture carry two
    # near misses it does not need.
    golden = _load(tmp_path, VALID_ENTRY)
    assert len(golden.questions) == 1


# ---- the new retrieval-shape categories ------------------------------


def test_the_new_retrieval_shapes_are_populated(golden):
    """Each shape needs enough cases to read as a row in a per-type table.

    The point of splitting these out of answerable_direct was to make a
    per-type failure visible. A bucket of one case cannot do that: it
    reports 0% or 100% and nothing in between.
    """
    for category in ("exact_term", "table", "code", "spans_two_chunks"):
        cases = golden.by_category(category)
        assert len(cases) >= 2, f"{category} has only {len(cases)} case(s)"


def test_spans_two_chunks_cases_really_span_two_chunks(golden):
    """The label must be true of the actual chunker, not merely asserted.

    This is the one new category whose name is a claim about system
    internals rather than about the question. Verified by chunking the
    expected documents with the production chunker and checking that no
    single CHILD carries every must_mention term -- so the embedded
    chunk that retrieves cannot contain the whole answer, and only the
    parent handed to the LLM can.

    If chunk sizes change this test fails, which is correct: the label
    would no longer be true and the case would silently stop testing
    what it was written for.
    """
    from app.services.chunking import chunk_pages

    documents = {(d.org, d.filename): d for d in golden.documents}
    cases = golden.by_category("spans_two_chunks")
    assert cases, "no spans_two_chunks cases"

    for case in cases:
        terms = [t.lower() for t in (case.rubric or {}).get("must_mention", ())]
        assert len(terms) >= 2, f"{case.id}: needs 2+ must_mention terms to span"

        pages = []
        for filename in case.expect_documents:
            pages.extend(documents[(case.org, filename)].pages)
        children = [
            child for parent in chunk_pages(pages) for child in parent.children
        ]
        assert children, f"{case.id}: expected documents produced no children"

        best = max(
            sum(term in child.content.lower() for term in terms)
            for child in children
        )
        assert best < len(terms), (
            f"{case.id}: one child chunk carries all of {terms}, so the case "
            f"does not span anything -- re-file it or tighten the rubric"
        )

        # And the terms must be findable at all, or the case is simply
        # unpassable and would read as a retrieval failure forever.
        corpus = " ".join(
            documents[(case.org, f)].content.lower() for f in case.expect_documents
        )
        for term in terms:
            assert term in corpus, f"{case.id}: must_mention {term!r} is not in the corpus"


def test_isolation_pairs_cover_both_directions(golden):
    """A leak that only flows one way would otherwise go unmeasured.

    Both original probes ran globex -> acme. The org filter is one
    predicate, so that is probably symmetric -- but "probably" is what
    the eval exists to replace.
    """
    asking_orgs = {c.org for c in golden.by_category("cross_org_isolation")}
    assert len(asking_orgs) >= 2, (
        f"every isolation probe is asked by {asking_orgs}, so only one "
        f"direction of leak is measured"
    )

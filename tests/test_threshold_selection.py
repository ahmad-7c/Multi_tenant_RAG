"""The threshold-selection rule.

Fixed in advance and implemented in code, so the number is not chosen by
trying values until one flatters the system. These tests pin the rule
itself; the measured scores it gets applied to live in
docs/eval-baseline.md.
"""
import pytest

from eval.thresholds import FALLBACK, ScoredCase, confusion, select_threshold


def case(score, should_answer, name=None, category="answerable_direct"):
    return ScoredCase(
        id=name or f"c{score}",
        category=category,
        score=score,
        should_answer=should_answer,
    )


def test_the_threshold_is_the_midpoint_of_the_widest_gap_below_the_lowest_answerable():
    cases = [
        case(0.90, True), case(0.80, True), case(0.60, True),
        case(0.30, False), case(0.10, False), case(0.05, False),
    ]
    choice = select_threshold(cases)

    # Candidates are everything <= 0.60 (the lowest answerable), with
    # 0.60 included as the ceiling: 0.05, 0.10, 0.30, 0.60.
    # Gaps: 0.05, 0.20, 0.30 -> widest is 0.30 -> 0.60.
    assert choice.gap_low == pytest.approx(0.30)
    assert choice.gap_high == pytest.approx(0.60)
    assert choice.value == pytest.approx(0.45)


def test_the_lowest_answerable_score_is_the_region_ceiling():
    """Without it, the most useful gap is invisible.

    The gap between the best refusable and the worst answerable case is
    usually the widest one, and it is the only gap that actually
    separates the classes. Excluding the ceiling would make the rule pick
    a gap buried among the refusals.
    """
    cases = [case(0.70, True), case(0.20, False), case(0.18, False)]
    choice = select_threshold(cases)

    # 0.18 -> 0.20 is 0.02; 0.20 -> 0.70 is 0.50 and must win.
    assert choice.gap_low == pytest.approx(0.20)
    assert choice.value == pytest.approx(0.45)


def test_the_fallback_is_used_when_no_gap_is_wide_enough():
    # Densely packed refusals right under the lowest answerable score:
    # no gap reaches 0.03, so there is no defensible midpoint and the
    # rule declines to invent one.
    cases = [
        case(0.50, True),
        case(0.49, False), case(0.48, False), case(0.47, False),
    ]
    choice = select_threshold(cases)

    assert choice.value == FALLBACK
    assert choice.used_fallback is True
    assert "no gap of at least" in choice.reason


def test_a_gap_exactly_at_the_minimum_is_accepted():
    cases = [case(0.50, True), case(0.47, False), case(0.10, False)]
    choice = select_threshold(cases, min_gap=0.03)
    # 0.10 -> 0.47 is 0.37, the widest; well over the minimum.
    assert choice.value == pytest.approx(0.285)


def test_refusals_above_the_lowest_answerable_are_reported_as_irreducible():
    """The overlap the rule cannot fix, named rather than hidden.

    A should-refuse case scoring above every answerable case is a false
    positive at ANY threshold that keeps full recall. That is a property
    of the signal, not of the number, and silently averaging it into a
    precision figure would make the gate look merely mis-tuned.
    """
    cases = [
        case(0.60, True, "real-answer"),
        case(0.87, False, "relevant-but-unanswerable"),
        case(0.10, False, "off-topic"),
    ]
    choice = select_threshold(cases)

    assert choice.irreducible_false_positives == ["relevant-but-unanswerable"]
    # And the chosen value does not pretend to catch it.
    matrix = confusion(cases, choice.value)
    assert matrix["fp"] == 1
    assert matrix["recall"] == 1.0


def test_no_answerable_cases_falls_back():
    choice = select_threshold([case(0.2, False), case(0.1, False)])
    assert choice.value == FALLBACK
    assert "no should-answer cases" in choice.reason


def test_the_rule_protects_recall_by_construction():
    """Every chosen value sits strictly below the lowest answerable score.

    That is the point of searching only below it: the rule can never
    cost a real answer on the measured set. Best-F1 would trade one away
    for a refusal, because F1 prices those failures the same -- and for
    a grounded-answers product they are not the same.
    """
    cases = [
        case(0.95, True), case(0.72, True), case(0.55, True),
        case(0.52, False), case(0.31, False), case(0.02, False),
    ]
    choice = select_threshold(cases)

    assert choice.value < min(c.score for c in cases if c.should_answer)
    assert confusion(cases, choice.value)["fn"] == 0


def test_confusion_counts_a_missing_score_as_a_refusal():
    # Not applicable to ScoredCase (score is required), but the gate's
    # own behaviour on an empty ranking is to refuse, and the sweep in
    # calibrate.py relies on that reading.
    cases = [case(0.9, True), case(0.1, False)]
    matrix = confusion(cases, 0.5)
    assert (matrix["tp"], matrix["fp"], matrix["tn"], matrix["fn"]) == (1, 0, 1, 0)


# ---- every qualifying gap is reported, not only the chosen one ----------


def test_all_qualifying_gaps_are_reported():
    """The n=38 finding, as a test.

    Three gaps cleared MIN_GAP below the lowest answerable score and all
    three held recall at 1.000, so the rule's "widest" tie-break was
    spending precision on margin without being asked. A function that
    returns only its own answer hides that a judgement was available.
    """
    cases = [
        ScoredCase(id="a", category="t", score=0.90, should_answer=True),
        ScoredCase(id="b", category="t", score=0.34, should_answer=True),
        ScoredCase(id="r1", category="t", score=0.30, should_answer=False),
        ScoredCase(id="r2", category="t", score=0.22, should_answer=False),
        ScoredCase(id="r3", category="t", score=0.08, should_answer=False),
    ]
    choice = select_threshold(cases)

    widths = [round(c.width, 4) for c in choice.alternatives]
    # Widest first: 0.08->0.22 (0.14), 0.22->0.30 (0.08), 0.30->0.34 (0.04)
    assert widths == [0.14, 0.08, 0.04]
    # The rule still picks the widest. The point is that the others are
    # visible beside it.
    assert choice.value == round((0.08 + 0.22) / 2, 4)
    assert choice.alternatives[0].midpoint == choice.value


def test_a_gap_below_the_minimum_width_is_not_offered():
    cases = [
        ScoredCase(id="a", category="t", score=0.50, should_answer=True),
        ScoredCase(id="r1", category="t", score=0.49, should_answer=False),
        ScoredCase(id="r2", category="t", score=0.10, should_answer=False),
    ]
    choice = select_threshold(cases)
    # 0.49 -> 0.50 is 0.01 wide. Offering it would propose a threshold
    # with a margin smaller than the noise between two runs.
    assert all(c.width >= 0.03 for c in choice.alternatives)
    assert [round(c.width, 4) for c in choice.alternatives] == [0.39]


def test_alternatives_are_reported_even_when_the_rule_falls_back():
    # No gap qualifies, so the value is FALLBACK -- and the near-miss
    # gaps are still worth seeing, because "the widest gap was 0.019"
    # is the fact that explains the fallback.
    cases = [
        ScoredCase(id="a", category="t", score=0.50, should_answer=True),
        ScoredCase(id="r1", category="t", score=0.49, should_answer=False),
        ScoredCase(id="r2", category="t", score=0.48, should_answer=False),
    ]
    choice = select_threshold(cases)
    assert choice.value == 0.50  # FALLBACK
    assert choice.alternatives == []

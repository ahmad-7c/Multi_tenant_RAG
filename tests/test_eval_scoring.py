"""The rubric check that replaced a forbidden substring.

The case that motivated it: acme-approval-7000 asks who approves a
7,000 USD report. A manager approves up to 5,000, above 5,000 needs
finance, above 25,000 needs the CFO. So "finance review is required; CFO
approval is not" is a CORRECT answer -- and `forbid_phrases: ["cfo"]`
scored it as a failure, because a substring test cannot tell "X is
required" from "X is not required".

These tests pin both directions, and the heuristic's documented limits
are tested as limits rather than pretended away.
"""
import pytest

from eval.scoring import check_phrases, check_rubric, is_asserted

RUBRIC = {"must_mention": ["finance"], "must_not_assert": ["cfo"]}


# ---- the answers that motivated the change ---------------------------


def test_an_answer_excluding_the_cfo_passes():
    answer = (
        "A direct manager approves it, and because it is above 5,000 USD it "
        "also requires finance review [1]. CFO approval is not required "
        "below 25,000 USD [1]."
    )
    result = check_rubric(answer, RUBRIC)

    assert result.passed, result.notes
    # Mentioned in order to rule out -- recorded, so a reviewer can see
    # what the heuristic decided instead of trusting it.
    assert "cfo" in result.excused


def test_an_answer_wrongly_requiring_the_cfo_fails():
    answer = (
        "A 7,000 USD report requires manager approval, finance review, and "
        "CFO sign-off [1]."
    )
    result = check_rubric(answer, RUBRIC)

    assert not result.passed
    assert "cfo" in result.forbidden_found


def test_an_answer_omitting_finance_fails():
    answer = "A direct manager approves expense reports up to 5,000 USD [1]."
    result = check_rubric(answer, RUBRIC)

    assert not result.passed
    assert "finance" in result.missing


def test_the_old_substring_check_would_have_failed_the_correct_answer():
    """Why the rubric exists, stated as a test.

    Same answer, both checks. If this ever stops holding, the rubric has
    become redundant and can go.
    """
    correct = (
        "Finance review is required for reports above 5,000 USD [1]. "
        "CFO approval is not needed at this amount [1]."
    )

    blunt = check_phrases(correct, expect_phrases=["finance"], forbid_phrases=["cfo"])
    assert not blunt.passed, "the substring check somehow passed"

    assert check_rubric(correct, RUBRIC).passed


# ---- is_asserted, directly -------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "The CFO must approve this.",
        "This needs CFO sign-off.",
        "Approval comes from the CFO.",
    ],
)
def test_a_plain_claim_is_asserted(answer):
    asserted, _ = is_asserted(answer, "cfo")
    assert asserted is True


@pytest.mark.parametrize(
    "answer",
    [
        "CFO approval is not required.",
        "This does not require the CFO.",
        "Finance review rather than the CFO.",
        "No CFO approval is needed.",
        "Finance reviews it, unlike the CFO tier.",
        "The CFO is not involved at this amount.",
    ],
)
def test_a_negated_or_contrasted_mention_is_not_asserted(answer):
    asserted, _ = is_asserted(answer, "cfo")
    assert asserted is False


def test_an_absent_term_is_not_asserted():
    asserted, why = is_asserted("A manager approves it.", "cfo")
    assert asserted is False
    assert why == "absent"


def test_one_unqualified_mention_is_enough_to_count():
    # An answer that both excludes and asserts a term is not correct
    # either, so the asserting mention wins.
    answer = "CFO approval is not required below 25,000. Send it to the CFO."
    asserted, _ = is_asserted(answer, "cfo")
    assert asserted is True


# ---- the documented limits, tested as limits -------------------------


def test_a_negation_beyond_the_window_is_missed():
    """A known limit, pinned so it is a decision rather than a surprise.

    The negation cue must fall within WINDOW characters before the term.
    A long intervening clause pushes it out of range and the mention
    reads as asserted. If this starts passing, the window grew and that
    is worth knowing.
    """
    answer = (
        "It is not the case that this particular expense report, given that "
        "it falls comfortably below the upper authorisation tier described "
        "elsewhere in the policy document, requires the CFO."
    )
    asserted, _ = is_asserted(answer, "cfo")

    assert asserted is True, "the window widened; re-check NEGATIONS"


def test_nested_negation_is_not_handled():
    answer = "It is not true that no CFO approval is needed."
    asserted, _ = is_asserted(answer, "cfo")
    # Reads the nearest cue ("no") and stops. Documented, not fixed:
    # handling this properly is parsing, and a transparent heuristic
    # that is wrong visibly beats an opaque one that is wrong rarely.
    assert asserted is False


# ---- the blunt check is still right for leaks ------------------------


def test_a_substring_ban_is_the_right_tool_for_an_isolation_leak():
    """Bluntness is correct where no grammar could excuse the phrase.

    Globex's corpus contains no sentence that could produce "thirty
    days", so its appearance is a leak regardless of surrounding words
    -- including "it is not thirty days", which still proves the other
    tenant's content reached the model.
    """
    leaked = "Your policy allows thirty days from the end of travel."
    result = check_phrases(leaked, forbid_phrases=["thirty days"])
    assert not result.passed

    negated_leak = "It is not thirty days."
    assert not check_phrases(negated_leak, forbid_phrases=["thirty days"]).passed


def test_citation_pages_must_match():
    from eval.scoring import check_citation_pages

    assert check_citation_pages([{"page": 2}], (2,)).passed
    # Right document, wrong page is still a wrong citation: it sends a
    # reader to the wrong place in a 4-page policy.
    bad = check_citation_pages([{"page": 1}], (2,))
    assert not bad.passed
    assert "page 2" in bad.missing

"""Scoring an answer against a golden label.

The interesting part is `check_rubric`, which exists because a bare
forbidden substring is wrong for synthesis cases.

acme-approval-7000 asks who approves a 7,000 USD report. The corpus says
a manager approves up to 5,000, above 5,000 needs finance review, and
above 25,000 needs the CFO. So the answer must reach "manager plus
finance" and must NOT claim the CFO is required. `forbid_phrases:
["cfo"]` fails both ways round:

    "Finance review is required; CFO approval is not."   -> scored WRONG
    "A manager, finance, and possibly the CFO."          -> scored right
                                                            if "cfo" were
                                                            spelled "C.F.O."

The first is a correct answer that mentions the CFO in order to exclude
it, which is exactly what a careful answer does. A substring test cannot
tell "X is required" from "X is not required", and that distinction is
the whole question.

So `must_not_assert` checks for the term in an ASSERTING context: the
term counts against the answer only when it is not governed by a nearby
negation or contrast. This is a heuristic, deliberately, with the limits
written down at NEGATIONS below. The alternative was an LLM judge, which
is more robust on phrasing and brings its own problems: cost per eval
run, a second model's failure modes inside the measurement, and
non-determinism in a number meant to be compared across runs. A
transparent heuristic that can be audited line by line is worth more
here than an opaque one that is right slightly more often -- and when it
is wrong, it is wrong visibly.
"""
import re
from dataclasses import dataclass, field

# Cues that flip or limit the claim attached to a term. Matched within
# WINDOW characters BEFORE the term.
#
# Known limits, stated rather than discovered later:
#   * cues after the term are missed ("the CFO is not involved") -- the
#     trailing form is caught by the suffix list below, not generally
#   * sarcasm, nested negation ("it is not true that no CFO...") and
#     conditionals ("only if the CFO agrees") are not handled
#   * a long clause can push a real negation outside the window
# When this heuristic and a human disagree, the human is right and the
# case should move to an explicit expect/forbid phrase pair.
NEGATIONS = (
    "not",
    "no",
    "never",
    "without",
    "rather than",
    "instead of",
    "unlike",
    "other than",
    "does not",
    "do not",
    "is not",
    "are not",
    "would not",
    "need not",
    "neither",
    "nor",
    "only above",
    "only for",
    "unless",
    "except",
    "beyond",
)

# Cues that appear AFTER the term and negate it.
TRAILING_NEGATIONS = (
    "is not required",
    "are not required",
    "is not needed",
    "not involved",
    "does not apply",
    "is not necessary",
    "only applies above",
    "only applies to",
)

WINDOW = 60
TRAILING_WINDOW = 40

# Sentence terminators. Negation is scoped to the clause it appears in.
_SENTENCE_END_RE = re.compile(r"[.!?;:]\s|\n")


@dataclass
class PhraseResult:
    passed: bool
    missing: list[str] = field(default_factory=list)
    forbidden_found: list[str] = field(default_factory=list)
    # Terms present but judged non-asserting, kept for the report so a
    # reviewer can see what the heuristic decided rather than trusting it.
    excused: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _occurrences(haystack: str, needle: str):
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index == -1:
            return
        yield index
        start = index + len(needle)


def is_asserted(answer: str, term: str) -> tuple[bool, str]:
    """Does `answer` actually CLAIM `term`, rather than exclude it?

    Returns (asserted, why). An absent term is not asserted.
    """
    text = answer.lower()
    term = term.lower()
    if term not in text:
        return False, "absent"

    for index in _occurrences(text, term):
        before = text[max(0, index - WINDOW): index]
        # A negation does not reach across a sentence boundary. Without
        # this, "CFO approval is not required below 25,000. Send it to
        # the CFO." excuses the SECOND mention using the first
        # sentence's "not" -- so an answer that both denies and then
        # asserts the term would pass.
        before = _SENTENCE_END_RE.split(before)[-1]
        after = text[index + len(term): index + len(term) + TRAILING_WINDOW]
        # Same boundary on the trailing side.
        after = _SENTENCE_END_RE.split(after)[0]

        negated_before = any(
            re.search(rf"\b{re.escape(cue)}\b", before) for cue in NEGATIONS
        )
        negated_after = any(cue in after for cue in TRAILING_NEGATIONS)

        if not negated_before and not negated_after:
            # One unqualified mention is enough to call it asserted: an
            # answer that both excludes and asserts a term is not a
            # correct answer either.
            return True, f"asserted near: ...{before[-40:]}[{term}]{after[:30]}..."

    return False, "present but negated or contrasted everywhere"


def check_rubric(answer: str, rubric: dict) -> PhraseResult:
    """Structured check for synthesis cases."""
    result = PhraseResult(passed=True)
    text = answer.lower()

    for term in rubric.get("must_mention") or ():
        if term.lower() not in text:
            result.passed = False
            result.missing.append(term)

    for term in rubric.get("must_not_assert") or ():
        asserted, why = is_asserted(answer, term)
        if asserted:
            result.passed = False
            result.forbidden_found.append(term)
            result.notes.append(f"{term}: {why}")
        elif term.lower() in text:
            # Mentioned in order to rule it out. A plain forbid-string
            # check would have failed the answer here, and would have
            # been wrong to.
            result.excused.append(term)
            result.notes.append(f"{term}: {why}")

    return result


def check_phrases(
    answer: str,
    expect_phrases=(),
    forbid_phrases=(),
) -> PhraseResult:
    """Plain substring check, for cases where bluntness is correct.

    Still the right tool for isolation probes: "thirty days" appearing in
    a Globex answer is a leak whatever the surrounding grammar, since
    Globex's corpus contains no sentence that could have produced it.
    """
    result = PhraseResult(passed=True)
    text = answer.lower()

    for phrase in expect_phrases:
        if phrase.lower() not in text:
            result.passed = False
            result.missing.append(phrase)

    for phrase in forbid_phrases:
        if phrase.lower() in text:
            result.passed = False
            result.forbidden_found.append(phrase)

    return result


def check_citation_pages(citations, expect_pages) -> PhraseResult:
    """Did the citations land on the expected page(s)?

    A citation with the right document and the wrong page is still a
    wrong citation: it sends a reader to the wrong place in a 4-page
    policy, and "the system cited the policy" is not what a reader
    needed.
    """
    result = PhraseResult(passed=True)
    if not expect_pages:
        return result

    cited_pages = sorted({c.get("page") for c in citations if c.get("page") is not None})
    for page in expect_pages:
        if page not in cited_pages:
            result.passed = False
            result.missing.append(f"page {page}")
    if not result.passed:
        result.notes.append(f"cited pages: {cited_pages or 'none'}")
    return result

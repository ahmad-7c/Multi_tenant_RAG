"""Choosing a gate threshold from measured scores.

The rule, fixed in advance so the number is not chosen by looking at
which value flatters the system:

  1. Take every score at or below the LOWEST should-answer score.
  2. Find the largest gap between consecutive values in that region.
  3. The threshold is that gap's midpoint.
  4. If no gap is at least MIN_GAP wide, fall back to FALLBACK.

Why "below the lowest answerable" rather than "best F1": this rule
maximises the margin to the nearest answerable case, so it protects
RECALL first. Best-F1 would happily sacrifice a real answer to win a
refusal, and F1 treats those as equal costs -- which for a
grounded-answers product they are not.

The rule cannot fix an overlap. When a should-refuse score sits ABOVE
the lowest answerable score, no threshold separates the classes and the
gap search never sees it: that case stays a false positive at any value
this function can return. That is a property of the signal, not of the
threshold, and `select_threshold` reports it rather than hiding it.
"""
from dataclasses import dataclass, field

MIN_GAP = 0.03
FALLBACK = 0.50


@dataclass(frozen=True)
class ScoredCase:
    id: str
    category: str
    score: float
    should_answer: bool


@dataclass
class ThresholdChoice:
    value: float
    reason: str
    gap_low: float | None = None
    gap_high: float | None = None
    gap_width: float | None = None
    lowest_answerable: float | None = None
    # should-refuse cases scoring at or above the lowest answerable
    # score. No threshold can exclude these while keeping full recall.
    irreducible_false_positives: list[str] = field(default_factory=list)

    @property
    def used_fallback(self) -> bool:
        return self.gap_width is None


def select_threshold(
    cases: list[ScoredCase], *, min_gap: float = MIN_GAP, fallback: float = FALLBACK
) -> ThresholdChoice:
    answerable = sorted(c.score for c in cases if c.should_answer)
    refusable = sorted(c.score for c in cases if not c.should_answer)

    if not answerable:
        return ThresholdChoice(
            value=fallback, reason="no should-answer cases had a score"
        )

    lowest_answerable = answerable[0]
    irreducible = [
        c.id for c in cases if not c.should_answer and c.score >= lowest_answerable
    ]

    # Candidates: everything at or below the lowest answerable score.
    # The lowest answerable score is INCLUDED as the region's ceiling --
    # otherwise the gap between the top refusable and the first
    # answerable case is invisible, and that gap is usually the widest
    # and the most useful one.
    region = sorted({s for s in refusable if s <= lowest_answerable} | {lowest_answerable})

    best_width = 0.0
    best_pair: tuple[float, float] | None = None
    for low, high in zip(region, region[1:]):
        width = high - low
        if width > best_width:
            best_width = width
            best_pair = (low, high)

    if best_pair is None or best_width < min_gap:
        return ThresholdChoice(
            value=fallback,
            reason=(
                f"no gap of at least {min_gap} below the lowest answerable "
                f"score ({lowest_answerable:.4f}); widest was "
                f"{best_width:.4f}"
            ),
            lowest_answerable=lowest_answerable,
            irreducible_false_positives=irreducible,
        )

    low, high = best_pair
    value = round((low + high) / 2, 4)
    return ThresholdChoice(
        value=value,
        reason=(
            f"midpoint of the widest gap below the lowest answerable score: "
            f"{low:.4f} -> {high:.4f} (width {best_width:.4f})"
        ),
        gap_low=low,
        gap_high=high,
        gap_width=best_width,
        lowest_answerable=lowest_answerable,
        irreducible_false_positives=irreducible,
    )


def confusion(cases: list[ScoredCase], threshold: float) -> dict:
    """What the chosen threshold would decide, for the record."""
    tp = fp = tn = fn = 0
    for case in cases:
        answers = case.score >= threshold
        if case.should_answer and answers:
            tp += 1
        elif case.should_answer:
            fn += 1
        elif answers:
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    return {
        "threshold": threshold,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "accuracy": (tp + tn) / len(cases) if cases else None,
    }


def score_table(cases: list[ScoredCase], label: str) -> str:
    """The full sorted list, both classes, every case named.

    Printed in full rather than summarised: a median hides which case
    sits at the boundary, and the boundary cases are the only ones that
    matter when choosing a threshold.
    """
    lines = [f"{label}"]
    for wanted, heading in ((True, "should ANSWER"), (False, "should REFUSE")):
        rows = sorted(
            (c for c in cases if c.should_answer is wanted),
            key=lambda c: -c.score,
        )
        lines.append("")
        lines.append(f"  {heading}  (n={len(rows)})")
        lines.append(f"    {'score':>7}  {'case':34} type")
        for case in rows:
            lines.append(f"    {case.score:>7.4f}  {case.id:34} {case.category}")
    return "\n".join(lines)

"""Citation validation.

The model emits bare markers like `[2]`. Those are resolved against the
block list the SERVER built, so a citation is either correct or dropped --
the model never gets to author provenance.

This is why citations are `[n]` rather than structured JSON. If the model
were asked to emit {"document": "...", "page": 4} it could produce a
plausible filename and page that appeared in no context block, and the
result would look exactly like a real citation. A bare integer has only
two possible fates: it maps to a block we supplied, or it does not exist.

Worth being precise about what this buys: a validated citation proves the
cited text was *retrieved and shown to the model*. It does NOT prove the
answer follows from it. The model can cite block [2] for a claim block [2]
does not support, and this module cannot tell. Citation correctness is
verifiable mechanically; answer correctness needs evaluation (Phase 4).
What validation does guarantee is that every citation a user can click
leads to real text in their own documents.
"""
import logging
import re
from dataclasses import dataclass

from app.services.prompting import ContextBlock

logger = logging.getLogger(__name__)

# Matches [1], [2][3], [4, 5] and [6,7] -- models produce all of these.
_MARKER_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

SNIPPET_CHARS = 300
# The passage is the whole parent the model read, for the source panel.
# Capped because a parent runs to ~500 tokens and the panel is a
# verification aid, not a document viewer.
PASSAGE_CHARS = 1200
# How much of a matched child to search for when its full text does not
# appear verbatim in the parent.
_MATCH_HEAD_CHARS = 120


@dataclass(frozen=True)
class Citation:
    marker: int
    document_id: object
    chunk_id: object
    document: str
    page: int
    snippet: str
    # --- added in B1, for the UI's source panel and relevance bar ---
    # All three default to None so entries cached BEFORE B1 still load:
    # the cache read path is `Citation(**cached_dict)`, and a required
    # field here would raise TypeError on every warm key -- turning a
    # deploy into an outage for as long as the TTL.
    passage: str | None = None
    # Which child chunk caused this parent to be retrieved, so the UI can
    # highlight the matched sentence inside the passage.
    matched_chunk_id: object = None
    # None whenever the score is not a calibrated relevance. An RRF score
    # is a rank reciprocal topping out near 1/(k+1), so rendered in a
    # 0..1 bar it reads as "almost no match" for every source of every
    # answer -- a plausible-looking number that means nothing.
    score: float | None = None


def _snippet(content: str) -> str:
    collapsed = " ".join(content.split())
    if len(collapsed) <= SNIPPET_CHARS:
        return collapsed
    # Cut at a word boundary so the snippet does not end mid-word.
    cut = collapsed[:SNIPPET_CHARS]
    space = cut.rfind(" ")
    return (cut[:space] if space > SNIPPET_CHARS // 2 else cut) + "..."


def _window_start(collapsed: str, matched: str | None) -> int:
    """Where the passage excerpt begins inside an over-long parent.

    Anchored at the parent's start unless the matched child sits past
    the cap, in which case the window FOLLOWS the match. A parent runs
    to ~500 tokens, well over PASSAGE_CHARS, so a start-anchored window
    shows a reader the wrong part of the right page whenever the match
    is in the back half: the highlight resolves to nothing and the
    excerpt visibly fails to contain the answer printed above it. The
    reader concludes the citation is wrong, which is the exact opposite
    of what the passage exists to establish.
    """
    if not matched:
        return 0
    needle = " ".join(matched.split())
    at = collapsed.find(needle)
    if at == -1 and len(needle) > _MATCH_HEAD_CHARS:
        # Children are overlapping token windows, so a child's text does
        # not always survive verbatim in the parent (the chunker may
        # have split mid-sentence). Fall back to its opening clause.
        at = collapsed.find(needle[:_MATCH_HEAD_CHARS])
    if at == -1 or at + len(needle) <= PASSAGE_CHARS:
        # Either the match cannot be located, or it already fits inside
        # a start-anchored window. Keep natural reading order.
        return 0
    # Put the match about a third of the way in, so there is leading
    # context without pushing the match itself out of the window.
    lead = max(0, (PASSAGE_CHARS - min(len(needle), PASSAGE_CHARS)) // 3)
    start = max(0, at - lead)
    # Prefer starting on a sentence boundary, when one falls inside the
    # lead-in we just allocated.
    prior = collapsed.rfind(". ", start, at)
    if prior != -1:
        start = prior + 2
    return start


def _passage(content: str, matched: str | None = None) -> str:
    """The parent passage, capped, cut at sentence ends, centred on the
    matched child.

    Cut on a sentence boundary rather than a character count because a
    passage ending mid-clause reads as corrupted data, not as an
    excerpt -- and the reader is using this text to check an answer, so
    it has to look trustworthy to be useful.

    The returned string INCLUDING its ellipses never exceeds
    PASSAGE_CHARS, because the cap is what the UI lays out against.
    """
    collapsed = " ".join(content.split())
    if len(collapsed) <= PASSAGE_CHARS:
        return collapsed

    start = _window_start(collapsed, matched)
    # An excerpt that does not start at the parent's start says so, or
    # the reader assumes the passage begins where the document does.
    lead = "..." if start > 0 else ""

    if len(collapsed) - start <= PASSAGE_CHARS - len(lead):
        # The window reaches the end of the parent: no trailing ellipsis
        # is needed, so its three characters go back to the text.
        return lead + collapsed[start:]

    # Reserve room for both ellipses up front.
    budget = PASSAGE_CHARS - len(lead) - 3
    body = collapsed[start : start + budget]
    end = max(body.rfind(". "), body.rfind("! "), body.rfind("? "))
    if end > budget // 2:
        # Keep the terminator, drop the trailing space. A clean sentence
        # end needs no trailing ellipsis.
        return lead + body[: end + 1]
    # No sentence break in the back half (a table, or one long clause):
    # fall back to a word boundary and say it was truncated.
    space = body.rfind(" ")
    return lead + (body[:space] if space > budget // 2 else body) + "..."


def validate_citations(
    answer: str, blocks: list[ContextBlock], *, expose_scores: bool = False
) -> tuple[str, list[Citation]]:
    """Resolve markers against the supplied blocks.

    Returns (cleaned_answer, citations). Markers that do not correspond to
    a supplied block are removed from the text entirely rather than left
    in place: a visible `[7]` that resolves to nothing is worse than no
    marker, because the reader assumes there is a source behind it.

    Citations are returned in the order the answer first cites them, which
    is the order a reader encounters them.

    `passage` is attached HERE, inside the cited-markers loop, which is
    what makes "passages only for cited sources" structural rather than
    a filter somebody has to remember to apply. The uncited blocks are
    the model's rejected context: the user never saw them, and shipping
    them would quietly widen what one question discloses.

    `expose_scores` defaults False -- the caller must assert that this
    ranking's scores are relevance (see `scores_are_relevance`). A
    default of True would publish RRF scores from every passthrough and
    every reranker outage.
    """
    by_marker = {block.marker: block for block in blocks}
    cited: dict[int, Citation] = {}
    dropped: list[int] = []

    def replace(match: re.Match) -> str:
        numbers = [int(part) for part in re.split(r"\s*,\s*", match.group(1))]
        kept = []
        for number in numbers:
            block = by_marker.get(number)
            if block is None:
                # Hallucinated or stale marker.
                dropped.append(number)
                continue
            kept.append(number)
            if number not in cited:
                cited[number] = Citation(
                    marker=number,
                    document_id=block.document_id,
                    chunk_id=block.parent_id,
                    document=block.filename,
                    page=block.page_number,
                    snippet=_snippet(block.content),
                    passage=_passage(block.content, block.matched_content),
                    matched_chunk_id=block.matched_chunk_id,
                    score=block.score if expose_scores else None,
                )
        if not kept:
            return ""
        return "".join(f"[{number}]" for number in kept)

    cleaned = _MARKER_RE.sub(replace, answer)
    # Removing a marker can leave " ." or a double space behind.
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    if dropped:
        # Logged, not raised: a wrong marker is the model's mistake and
        # the answer is still useful without it. Worth watching, though --
        # a rising rate means the prompt or the model changed.
        logger.warning(
            "dropped %s citation marker(s) not in the supplied set: %s",
            len(dropped),
            sorted(set(dropped)),
        )

    return cleaned, [cited[number] for number in sorted(cited, key=lambda n: cleaned.find(f"[{n}]"))]

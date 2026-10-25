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


@dataclass(frozen=True)
class Citation:
    marker: int
    document_id: object
    chunk_id: object
    document: str
    page: int
    snippet: str


def _snippet(content: str) -> str:
    collapsed = " ".join(content.split())
    if len(collapsed) <= SNIPPET_CHARS:
        return collapsed
    # Cut at a word boundary so the snippet does not end mid-word.
    cut = collapsed[:SNIPPET_CHARS]
    space = cut.rfind(" ")
    return (cut[:space] if space > SNIPPET_CHARS // 2 else cut) + "..."


def validate_citations(
    answer: str, blocks: list[ContextBlock]
) -> tuple[str, list[Citation]]:
    """Resolve markers against the supplied blocks.

    Returns (cleaned_answer, citations). Markers that do not correspond to
    a supplied block are removed from the text entirely rather than left
    in place: a visible `[7]` that resolves to nothing is worse than no
    marker, because the reader assumes there is a source behind it.

    Citations are returned in the order the answer first cites them, which
    is the order a reader encounters them.
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

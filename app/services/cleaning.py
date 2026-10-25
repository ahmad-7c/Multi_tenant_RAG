"""Pages -> cleaner pages.

PDF extraction gives you text with the page's *visual* layout baked in:
hard line breaks at the right margin, words hyphenated across lines, and
whatever header/footer the template repeats. Each of those actively
degrades retrieval:

  - "manage-\\nment" tokenizes into two fragments that carry none of the
    meaning of "management", so the chunk never matches a query about it.
  - A running header repeated on 40 pages becomes one of the most
    statistically prominent phrases in the document, pulling unrelated
    chunks toward any query that happens to share a word with it.
  - Hard line breaks inside a sentence split it across what the chunker
    would otherwise treat as separate paragraphs.

Everything here is line-structure-aware: code fences, tables, headings and
list items must keep their own line breaks, because the chunker uses those
breaks to decide what must stay whole.
"""
import re
from collections import Counter

from app.core.config import settings
from app.services.extraction import Page

_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6}\s+\S|=+\s*$|-{3,}\s*$)")
_LIST_RE = re.compile(r"^\s*([-*+]\s+|\d+[.)]\s+)")
_TABLE_RE = re.compile(r"^\s*\|.*\|\s*$|^\s*\|")
# A line break mid-word: lowercase letter, hyphen, end of line, then a
# lowercase continuation. Requiring lowercase on BOTH sides is what keeps
# "Smith-\nJones" and "well-\nknown" from being silently welded together.
_HYPHEN_BREAK_RE = re.compile(r"([a-z])-\n([a-z])")
_DIGITS_RE = re.compile(r"\d+")


def _is_structural(line: str) -> bool:
    """True for a line whose own newline carries meaning."""
    stripped = line.strip()
    if not stripped:
        return True
    return bool(
        _HEADING_RE.match(line) or _LIST_RE.match(line) or _TABLE_RE.match(line)
    )


def _normalise_for_comparison(line: str) -> str:
    """Key used to decide whether two pages show the 'same' header.

    Digits are collapsed because the single most common footer in existence
    is "Page 3 of 10" -- literally different on every page. Comparing raw
    text would never detect it.
    """
    return _DIGITS_RE.sub("#", " ".join(line.split())).casefold()


def _repeated_edge_lines(pages: list[Page]) -> set[str]:
    """Normalised keys of lines that look like running headers/footers."""
    if len(pages) < settings.CLEAN_MIN_PAGES_FOR_HEADER_DETECTION:
        # With only a page or two there is no way to tell a running header
        # from a line that legitimately appears on both.
        return set()

    edge = settings.CLEAN_EDGE_LINES
    counts: Counter[str] = Counter()
    for page in pages:
        lines = [line for line in page.text.replace("\r\n", "\n").split("\n") if line.strip()]
        # Only the top and bottom of each page are candidates. A phrase
        # repeated in the middle of many pages is a real recurring topic,
        # not page furniture.
        candidates = lines[:edge] + lines[-edge:]
        # set() so a line appearing twice on ONE page still counts once --
        # otherwise a short page could single-handedly cross the threshold.
        for line in {_normalise_for_comparison(line) for line in candidates}:
            # Length cap: running headers and footers are short. Without it,
            # digit normalization makes any prose line that merely contains
            # a number ("...revenue grew 12% in 2024...") collide with the
            # same sentence on another page, and real content gets deleted.
            if line and len(line) <= settings.CLEAN_MAX_HEADER_CHARS:
                counts[line] += 1

    threshold = max(2, int(len(pages) * settings.CLEAN_REPEAT_RATIO))
    return {line for line, count in counts.items() if count >= threshold}


def _strip_repeated_lines(text: str, repeated: set[str]) -> str:
    if not repeated:
        return text
    kept = []
    lines = text.split("\n")
    edge = settings.CLEAN_EDGE_LINES
    # Index positions that are eligible for removal, counting only
    # non-blank lines, so a header preceded by a blank line is still at the
    # "top" of the page.
    non_blank = [i for i, line in enumerate(lines) if line.strip()]
    eligible = set(non_blank[:edge]) | set(non_blank[-edge:])
    for i, line in enumerate(lines):
        if i in eligible and _normalise_for_comparison(line) in repeated:
            continue
        kept.append(line)

    # Safety net: never strip a page down to nothing. If the heuristic
    # matched every line on a page it is wrong about that page, and losing
    # the content outright is far worse than leaving a header in.
    if not any(line.strip() for line in kept):
        return text
    return "\n".join(kept)


def _join_wrapped_lines(text: str) -> str:
    """Join single newlines inside a paragraph, keep blank lines.

    Walks line by line rather than using a regex so that code fences,
    tables, headings and list items can be left alone -- a global
    "replace single \\n with space" would destroy all four.
    """
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False

    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue

        if in_fence:
            # Inside a fence every newline is content.
            out.append(line)
            continue

        if _is_structural(line):
            out.append(line)
            continue

        # A plain prose line: append to the previous one if that was also
        # plain prose, otherwise start a new block.
        if out and out[-1].strip() and not _is_structural(out[-1]) and not _joined_block_closed(out):
            previous = out[-1].rstrip()
            # A line ending in a hyphen that survived de-hyphenation is a
            # real compound split at the margin ("Smith-\nJones"). Join
            # without a space, or the hyphen ends up dangling: "Smith- Jones".
            separator = "" if previous.endswith("-") else " "
            out[-1] = previous + separator + line.strip()
        else:
            out.append(line)

    return "\n".join(out)


def _joined_block_closed(out: list[str]) -> bool:
    # The previous emitted line was a fence delimiter, so prose after it
    # starts a fresh block rather than continuing the fence line.
    return bool(_FENCE_RE.match(out[-1]))


def clean_page_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # De-hyphenate BEFORE joining lines: the pattern anchors on the newline
    # that the join is about to remove.
    text = _HYPHEN_BREAK_RE.sub(r"\1\2", text)
    text = _join_wrapped_lines(text)
    # Collapse runs of blank lines to exactly one paragraph break. Three or
    # more carry no extra meaning and only inflate token counts.
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Trailing spaces left by the join step.
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


def clean_pages(pages: list[Page]) -> list[Page]:
    repeated = _repeated_edge_lines(pages)
    return [
        Page(page.page_number, clean_page_text(_strip_repeated_lines(page.text, repeated)))
        for page in pages
    ]

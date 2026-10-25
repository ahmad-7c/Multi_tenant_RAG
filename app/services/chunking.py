"""Pages -> parent chunks, each with its child chunks.

Two levels, two jobs:

  - CHILDREN are embedded and searched. They are small so that a single
    fact is not diluted by 500 tokens of surrounding prose.
  - PARENTS are what the LLM reads. They are large so the model sees the
    paragraph a fact lives in, not a 125-token sliver of it.

The governing rule is that **tiktoken measures and never cuts**. Token
counts decide *when* to stop packing; the actual boundaries are always
structure the writer put there -- a blank line, a sentence-ending
punctuation mark, a table row, a line of code. A window slid blindly over
a token stream produces chunks that start mid-word and end mid-clause,
and those embed as noise.
"""
import re
from dataclasses import dataclass, field
from functools import lru_cache

import tiktoken

from app.core.config import settings


@lru_cache(maxsize=4)
def _encoding_for(model: str):
    try:
        # Resolve via the configured model name, never a hardcoded
        # encoding, so the token counts match whatever actually embeds.
        return tiktoken.encoding_for_model(model)
    except KeyError:
        # A model tiktoken has not heard of yet (or a non-OpenAI one).
        # cl100k_base is close enough for budgeting -- being a few percent
        # off a budget is harmless; refusing to chunk is not.
        return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding_for(settings.EMBEDDING_MODEL).encode(text))


_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(\S.*)$")
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_TABLE_ROW_RE = re.compile(r"^\s*\|")
# A markdown table's separator row: | --- | :--: | etc.
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:\-|]+\|?\s*$")
# Sentence end: punctuation followed by whitespace. Lookbehind keeps the
# punctuation attached to the sentence it terminates.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?:;])\s+")


# ---------------------------------------------------------------- blocks ----

PARAGRAPH = "paragraph"
HEADING = "heading"
TABLE = "table"
CODE = "code"
LIST = "list"


@dataclass
class Block:
    kind: str
    text: str
    page_number: int

    @property
    def is_atomic(self) -> bool:
        """Blocks that must never be split across parents.

        A table cut in half loses its header on one side; a code fence cut
        in half is unreadable and breaks markdown rendering everywhere
        downstream.
        """
        return self.kind in (TABLE, CODE)


def _split_blocks(page_number: int, text: str) -> list[Block]:
    """One page of cleaned text -> atomic blocks.

    A line-by-line state machine rather than a regex split, because the
    meaning of a line depends on what came before it: a `|` inside a code
    fence is code, not a table row.
    """
    lines = text.split("\n")
    blocks: list[Block] = []
    buffer: list[str] = []
    kind = PARAGRAPH

    def flush() -> None:
        nonlocal buffer, kind
        if buffer:
            body = "\n".join(buffer).strip("\n")
            if has_searchable_content(body):
                blocks.append(Block(kind, body, page_number))
        buffer = []
        kind = PARAGRAPH

    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            if in_fence:
                # Closing delimiter belongs to the block it closes.
                buffer.append(line)
                in_fence = False
                flush()
            else:
                flush()
                kind = CODE
                buffer.append(line)
                in_fence = True
            continue

        if in_fence:
            buffer.append(line)
            continue

        stripped = line.strip()

        if not stripped:
            flush()
            continue

        if _HEADING_RE.match(line):
            flush()
            if has_searchable_content(stripped):
                blocks.append(Block(HEADING, stripped, page_number))
            continue

        if _TABLE_ROW_RE.match(line):
            if kind != TABLE:
                flush()
                kind = TABLE
            buffer.append(line)
            continue

        # A non-table line ends a table.
        if kind == TABLE:
            flush()

        buffer.append(line)

    if in_fence:
        # Unterminated fence: keep it as code anyway rather than
        # reinterpreting half a file as prose.
        kind = CODE
    flush()
    return blocks


# --------------------------------------------------------------- chunks -----


@dataclass
class ChildChunk:
    content: str
    page_number: int
    token_count: int
    chunk_index: int


@dataclass
class ParentChunk:
    content: str
    page_number: int
    token_count: int
    chunk_index: int
    heading: str | None = None
    children: list[ChildChunk] = field(default_factory=list)


def _heading_prefix(heading: str | None, body: str) -> str:
    """Prepend the nearest heading unless the body already carries it."""
    if not heading:
        return body
    if heading in body:
        # Already present (the heading block started this parent), so
        # prepending again would double it and waste budget.
        return body
    return f"{heading}\n\n{body}"


def has_searchable_content(text: str) -> bool:
    """True if the text contains anything a query could ever match.

    A chunk of pure punctuation or whitespace is not merely useless -- it
    is harmful: it occupies an index slot, costs an embedding call, and
    makes a document look successfully ingested when nothing in it is
    findable.
    """
    return any(ch.isalnum() for ch in text)


def _split_sentences(text: str) -> list[str]:
    parts = [p for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
    return parts or ([text] if text.strip() else [])


def _pack_units(units: list[str], budget: int, joiner: str) -> list[str]:
    """Greedily pack whole units into groups of at most `budget` tokens.

    A unit that exceeds the budget on its own is emitted alone and
    oversized: the alternative is cutting inside it, which is the thing
    this module exists to avoid.
    """
    groups: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for unit in units:
        unit_tokens = count_tokens(unit)
        if current and current_tokens + unit_tokens > budget:
            groups.append(joiner.join(current))
            current = []
            current_tokens = 0
        current.append(unit)
        current_tokens += unit_tokens

    if current:
        groups.append(joiner.join(current))
    return groups


def _table_header(block_text: str) -> str:
    """The header row (plus separator) of a markdown table."""
    lines = [line for line in block_text.split("\n") if line.strip()]
    if not lines:
        return ""
    header = [lines[0]]
    if len(lines) > 1 and _TABLE_SEP_RE.match(lines[1]):
        header.append(lines[1])
    return "\n".join(header)


def _children_for_table(block_text: str, budget: int) -> list[str]:
    header = _table_header(block_text)
    header_lines = header.split("\n") if header else []
    rows = [
        line
        for line in block_text.split("\n")
        if line.strip() and line not in header_lines
    ]
    header_tokens = count_tokens(header) if header else 0
    # Rows are the units; the header is repeated into every group, so the
    # per-group row budget is what is left after paying for it.
    row_budget = max(budget - header_tokens, max(1, budget // 4))
    groups = _pack_units(rows, row_budget, "\n")
    if not header:
        return groups
    # Repeating the header is the whole point: "| Region7 | 70 | 77 |" on
    # its own answers no question.
    return [f"{header}\n{group}" for group in groups]


def _children_for_code(block_text: str, budget: int) -> list[str]:
    lines = block_text.split("\n")
    # Lines, not tokens: cutting inside a line of code produces something
    # that is neither readable nor runnable.
    return _pack_units(lines, budget, "\n")


def _sentence_windows(text: str, budget: int, overlap: int) -> list[str]:
    """Sliding window over SENTENCES, with overlap measured in tokens.

    Overlap exists so a fact sitting on a boundary still appears whole
    inside at least one child's embedding.
    """
    sentences = _split_sentences(text)
    if not sentences:
        return []

    windows: list[str] = []
    start = 0
    while start < len(sentences):
        end = start
        tokens = 0
        while end < len(sentences):
            sentence_tokens = count_tokens(sentences[end])
            if end > start and tokens + sentence_tokens > budget:
                break
            tokens += sentence_tokens
            end += 1
        windows.append(" ".join(sentences[start:end]))

        if end >= len(sentences):
            break

        # Step back over whole sentences until `overlap` tokens are
        # re-included. Overlap is a token budget but the unit of movement
        # is still a sentence.
        back = end - 1
        carried = 0
        while back > start and carried + count_tokens(sentences[back]) <= overlap:
            carried += count_tokens(sentences[back])
            back -= 1
        start = max(back, start + 1)

    return windows


def _build_children(parent: ParentChunk, blocks: list[Block]) -> list[ChildChunk]:
    budget = settings.CHILD_TOKENS
    overlap = settings.CHILD_OVERLAP
    pieces: list[str] = []

    for block in blocks:
        if block.kind == TABLE:
            pieces.extend(_children_for_table(block.text, budget))
        elif block.kind == CODE:
            pieces.extend(_children_for_code(block.text, budget))
        elif block.kind == HEADING:
            # A heading alone is not a retrievable unit; it is already
            # prepended to every sibling child below.
            continue
        else:
            pieces.extend(_sentence_windows(block.text, budget, overlap))

    children: list[ChildChunk] = []
    for index, piece in enumerate(p for p in pieces if has_searchable_content(p)):
        content = _heading_prefix(parent.heading, piece)
        children.append(
            ChildChunk(
                content=content,
                page_number=parent.page_number,
                token_count=count_tokens(content),
                chunk_index=index,
            )
        )

    if not children and has_searchable_content(parent.content):
        # A parent with no children is unsearchable, which silently makes
        # part of the document unreachable. Fall back to the parent itself.
        children.append(
            ChildChunk(
                content=parent.content,
                page_number=parent.page_number,
                token_count=parent.token_count,
                chunk_index=0,
            )
        )
    return children


def chunk_pages(pages) -> list[ParentChunk]:
    blocks: list[Block] = []
    for page in pages:
        blocks.extend(_split_blocks(page.page_number, page.text))

    parents: list[ParentChunk] = []
    group: list[Block] = []
    group_tokens = 0
    heading: str | None = None
    group_heading: str | None = None

    def flush_group() -> None:
        nonlocal group, group_tokens, group_heading
        if not group:
            return
        body = "\n\n".join(b.text for b in group)
        content = _heading_prefix(group_heading, body)
        parent = ParentChunk(
            content=content,
            page_number=group[0].page_number,
            token_count=count_tokens(content),
            chunk_index=len(parents),
            heading=group_heading,
        )
        parent.children = _build_children(parent, list(group))
        parents.append(parent)
        group = []
        group_tokens = 0

    for block in blocks:
        if block.kind == HEADING:
            # A heading is a hard boundary: parents are the LLM's context,
            # and one parent spanning two sections hands the model text
            # that straddles topics.
            flush_group()
            heading = block.text
            group_heading = heading
            group = [block]
            group_tokens = count_tokens(block.text)
            continue

        # A parent never spans pages. It carries a single page_number, and
        # that number ends up in a citation the reader is invited to check
        # -- so a parent built from pages 6 and 7 would cite page 6 for a
        # fact that is printed on page 7. Wasting a little budget at a page
        # boundary is cheaper than a citation that does not hold up.
        if group and block.page_number != group[0].page_number:
            flush_group()
            group_heading = heading

        block_tokens = count_tokens(block.text)

        # An atomic block that does not fit gets a parent of its own rather
        # than being split. An oversized parent is a cost; a halved table
        # is a correctness bug.
        if block.is_atomic and block_tokens > settings.PARENT_TOKENS:
            flush_group()
            group_heading = heading
            group = [block]
            flush_group()
            group_heading = heading
            continue

        if group and group_tokens + block_tokens > settings.PARENT_TOKENS:
            flush_group()
            group_heading = heading

        # A single prose block larger than the budget is split on sentence
        # boundaries -- the only place in this module where a block is
        # divided, and still never mid-sentence.
        if not group and block_tokens > settings.PARENT_TOKENS and not block.is_atomic:
            for piece in _pack_units(
                _split_sentences(block.text), settings.PARENT_TOKENS, " "
            ):
                group = [Block(block.kind, piece, block.page_number)]
                group_heading = heading
                flush_group()
            group_heading = heading
            continue

        group.append(block)
        group_tokens += block_tokens

    flush_group()
    return parents

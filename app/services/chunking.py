import re
from dataclasses import dataclass

import tiktoken

# text-embedding-3-small's tokenizer -- counting against the actual
# embedding model name (not a hardcoded encoding) keeps this correct if
# OpenAI ever remaps which encoding backs this model.
_ENCODING = tiktoken.encoding_for_model("text-embedding-3-small")

PARENT_TARGET_TOKENS = 500
CHILD_TARGET_TOKENS = 125  # midpoint of the requested 100-150 token range
CHILD_OVERLAP_TOKENS = 20

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")


def count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text))


@dataclass
class Block:
    page_number: int
    text: str
    is_table: bool = False
    is_code: bool = False


def _split_page_into_blocks(page_number: int, text: str) -> list[Block]:
    """Split one page's text into atomic blocks: paragraphs (boundaries at
    blank lines and before headings), plus table and code-fence regions
    captured as single blocks each. Packing these into parents later never
    slices through the middle of a block, so a table/code fence can never
    end up split across two parent chunks.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[Block] = []
    buffer: list[str] = []
    mode = "normal"  # "normal" | "table" | "code"

    def flush() -> None:
        if not buffer:
            return
        content = "\n".join(buffer).strip()
        if content:
            blocks.append(
                Block(page_number, content, is_table=(mode == "table"), is_code=(mode == "code"))
            )
        buffer.clear()

    for line in lines:
        stripped = line.strip()

        if mode == "code":
            buffer.append(line)
            if stripped.startswith("```"):
                flush()
                mode = "normal"
            continue

        if stripped.startswith("```"):
            flush()
            mode = "code"
            buffer.append(line)
            continue

        is_table_row = "|" in stripped and stripped != ""

        if mode == "table" and not is_table_row:
            flush()
            mode = "normal"
            # falls through: this line is handled as normal/heading/blank below

        if is_table_row:
            if mode != "table":
                flush()
                mode = "table"
            buffer.append(line)
            continue

        if stripped == "":
            flush()
            continue

        if _HEADING_RE.match(line) and buffer:
            flush()

        buffer.append(line)

    flush()
    return blocks


@dataclass
class ParentChunk:
    page_number: int
    content: str
    token_count: int
    chunk_index: int


def build_parent_chunks(pages: list[tuple[int, str]]) -> list[ParentChunk]:
    """Pack atomic blocks (paragraphs/headings/tables/code blocks) into
    ~PARENT_TARGET_TOKENS-token parent chunks. A block larger than the
    target becomes its own oversized parent rather than being split --
    the one accepted trade-off for the "never split a table or code
    block" rule.
    """
    all_blocks: list[Block] = []
    for page_number, text in pages:
        all_blocks.extend(_split_page_into_blocks(page_number, text))

    parents: list[ParentChunk] = []
    current: list[Block] = []
    current_tokens = 0
    chunk_index = 0

    def flush_parent() -> None:
        nonlocal current, current_tokens, chunk_index
        if not current:
            return
        content = "\n\n".join(b.text for b in current)
        parents.append(
            ParentChunk(
                page_number=current[0].page_number,
                content=content,
                token_count=count_tokens(content),
                chunk_index=chunk_index,
            )
        )
        chunk_index += 1
        current = []
        current_tokens = 0

    for block in all_blocks:
        block_tokens = count_tokens(block.text)
        is_heading_start = _HEADING_RE.match(block.text.splitlines()[0]) is not None

        # A heading starts a fresh parent instead of being appended to
        # whatever came before -- keeps each parent topically coherent,
        # which matters because parents (not children) are what gets sent
        # to the LLM as context.
        if current and (
            current_tokens + block_tokens > PARENT_TARGET_TOKENS
            or is_heading_start
        ):
            flush_parent()

        current.append(block)
        current_tokens += block_tokens

    flush_parent()
    return parents


@dataclass
class ChildChunk:
    page_number: int
    content: str
    token_count: int
    chunk_index: int


def build_child_chunks(parent: ParentChunk) -> list[ChildChunk]:
    """Slide a CHILD_TARGET_TOKENS window over the parent's token stream
    with CHILD_OVERLAP_TOKENS of overlap between siblings, so a fact
    sitting on a child boundary still appears whole in at least one
    child's embedding. Every child inherits the parent's page_number.
    """
    tokens = _ENCODING.encode(parent.content)
    if not tokens:
        return []

    step = CHILD_TARGET_TOKENS - CHILD_OVERLAP_TOKENS
    children: list[ChildChunk] = []
    start = 0
    chunk_index = 0

    while start < len(tokens):
        end = min(start + CHILD_TARGET_TOKENS, len(tokens))
        window = tokens[start:end]
        children.append(
            ChildChunk(
                page_number=parent.page_number,
                content=_ENCODING.decode(window),
                token_count=len(window),
                chunk_index=chunk_index,
            )
        )
        chunk_index += 1
        if end == len(tokens):
            break
        start += step

    return children

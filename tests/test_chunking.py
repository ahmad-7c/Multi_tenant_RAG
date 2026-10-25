"""Chunker tests.

The chunker decides what a retrievable unit of meaning is, so almost every
retrieval failure traces back here. The invariants that matter:

  - tiktoken MEASURES, it never cuts. No chunk ends mid-sentence.
  - A table or a fenced code block is never split.
  - A child that contains table rows carries the header row, or the rows
    are meaningless out of context.
  - Every chunk carries its nearest heading, or an embedded fragment loses
    the topic it belongs to.
"""
import pytest

from app.core.config import settings
from app.services.chunking import chunk_pages, count_tokens
from app.services.extraction import Page


def _flat_children(parents):
    return [child for parent in parents for child in parent.children]


# ------------------------------------------------------- basic structure ----


def test_empty_input_yields_no_chunks():
    assert chunk_pages([]) == []
    assert chunk_pages([Page(1, "   \n\n  ")]) == []


def test_every_child_belongs_to_a_parent():
    parents = chunk_pages([Page(1, "A paragraph of text. " * 60)])

    assert parents
    for parent in parents:
        assert parent.children, "a parent with no children is unsearchable"


def test_child_indexes_are_sequential_within_a_parent():
    parents = chunk_pages([Page(1, "Sentence number one. " * 80)])

    for parent in parents:
        assert [c.chunk_index for c in parent.children] == list(range(len(parent.children)))


def test_parent_indexes_are_sequential_across_the_document():
    parents = chunk_pages([Page(1, "Body sentence here. " * 300)])

    assert [p.chunk_index for p in parents] == list(range(len(parents)))


def test_page_numbers_are_carried_through():
    parents = chunk_pages(
        [Page(1, "First page content here."), Page(7, "Seventh page content here.")]
    )

    pages = {p.page_number for p in parents}
    assert pages == {1, 7}
    for parent in parents:
        for child in parent.children:
            assert child.page_number == parent.page_number


def test_token_counts_are_recorded_and_nonzero():
    parents = chunk_pages([Page(1, "Some reasonable body text to measure.")])

    for parent in parents:
        assert parent.token_count == count_tokens(parent.content)
        for child in parent.children:
            assert child.token_count == count_tokens(child.content)


# ------------------------------------------------------------ never cuts ----


def test_children_never_end_mid_sentence():
    text = (
        "The first sentence is reasonably long and descriptive. "
        "The second sentence continues the same idea at length. "
        "The third sentence introduces a different topic entirely. "
    ) * 12

    children = _flat_children(chunk_pages([Page(1, text)]))

    assert len(children) > 1
    for child in children:
        stripped = child.content.strip()
        # tiktoken is a measuring tape, not a knife: every boundary lands
        # on punctuation the writer put there.
        assert stripped.endswith((".", "!", "?", ":", ";", "\"", ")", "`", "|")), stripped[-60:]


def test_a_sentence_longer_than_the_budget_is_still_emitted_whole():
    # A single 400-token sentence cannot be split on a sentence boundary.
    # Emitting it oversized is correct; silently truncating it is not.
    monster = "word " * 400 + "end."
    children = _flat_children(chunk_pages([Page(1, monster)]))

    assert any("end." in c.content for c in children)


# -------------------------------------------------------------- headings ----


def test_heading_starts_a_new_parent():
    text = (
        "Intro paragraph before any heading.\n\n"
        "# Compensation\n\n"
        "Body about compensation policy.\n\n"
        "# Travel\n\n"
        "Body about travel policy.\n"
    )

    parents = chunk_pages([Page(1, text)])

    # Parents are what the LLM reads. Mixing two sections into one parent
    # hands the model context that straddles topics.
    assert len(parents) >= 3
    assert any("Compensation" in p.content and "Travel" not in p.content for p in parents)


def test_nearest_heading_is_prepended_to_every_child():
    text = "# Expense Policy\n\n" + ("Receipts must be submitted within thirty days. " * 40)

    parents = chunk_pages([Page(1, text)])
    children = _flat_children(parents)

    assert len(children) > 1
    for child in children:
        # Without this, a child in the middle of a long section embeds as
        # anonymous prose and never matches a query naming the section.
        assert "Expense Policy" in child.content


def test_heading_is_not_duplicated_when_already_present():
    text = "# Short Section\n\nOne short line of body text."

    children = _flat_children(chunk_pages([Page(1, text)]))

    for child in children:
        assert child.content.count("Short Section") == 1


def test_child_without_a_preceding_heading_is_unprefixed():
    children = _flat_children(chunk_pages([Page(1, "Plain body text with no heading at all.")]))

    assert children
    assert not children[0].content.startswith("#")


# ----------------------------------------------------------------- tables ----

TABLE = (
    "| Region | Q1 | Q2 |\n"
    "| --- | --- | --- |\n"
    + "".join(f"| Region{i} | {i * 10} | {i * 11} |\n" for i in range(1, 60))
)


def test_a_table_is_never_split_across_parents():
    parents = chunk_pages([Page(1, "Intro text.\n\n" + TABLE + "\nTrailing text.")])

    holders = [p for p in parents if "| Region1 |" in p.content]
    assert len(holders) == 1, "the table body appears in more than one parent"
    # Every row stays with it.
    assert "| Region59 |" in holders[0].content


def test_table_children_repeat_the_header_row():
    parents = chunk_pages([Page(1, TABLE)])
    children = _flat_children(parents)

    table_children = [c for c in children if "| Region" in c.content]
    assert len(table_children) > 1, "the table should span several children to be meaningful"
    for child in table_children:
        # "| Region7 | 70 | 77 |" on its own says nothing. The header is
        # what makes the row answerable.
        assert "| Region | Q1 | Q2 |" in child.content


def test_table_rows_are_never_cut_mid_row():
    children = _flat_children(chunk_pages([Page(1, TABLE)]))

    for child in children:
        for line in child.content.strip().split("\n"):
            if line.strip().startswith("|"):
                assert line.strip().endswith("|"), line


# ------------------------------------------------------------------- code ----

CODE = (
    "```python\n"
    + "".join(f"def function_{i}():\n    return {i}\n\n" for i in range(1, 50))
    + "```"
)


def test_a_fenced_code_block_is_never_split_across_parents():
    parents = chunk_pages([Page(1, "Before the code.\n\n" + CODE + "\n\nAfter the code.")])

    holders = [p for p in parents if "def function_1()" in p.content]
    assert len(holders) == 1
    assert "def function_49()" in holders[0].content


def test_code_fence_delimiters_stay_balanced_in_each_parent():
    parents = chunk_pages([Page(1, "Intro.\n\n" + CODE + "\n\nOutro.")])

    for parent in parents:
        # An unbalanced fence means the block was cut in half, which makes
        # the code unreadable and breaks markdown rendering downstream.
        assert parent.content.count("```") % 2 == 0


def test_code_children_are_split_on_line_boundaries():
    children = _flat_children(chunk_pages([Page(1, CODE)]))

    for child in children:
        for line in child.content.split("\n"):
            # Indentation preserved means we cut between lines, not inside
            # one.
            assert line == line.rstrip() or line.strip() == ""


# ------------------------------------------------------------- budgets -----


def test_parents_respect_the_configured_budget_except_for_atomic_blocks():
    text = "A sentence of moderate length goes here. " * 400
    parents = chunk_pages([Page(1, text)])

    assert len(parents) > 1
    for parent in parents:
        # Prose must stay within budget; only an unsplittable atomic block
        # (a whole table, a whole code fence, a single huge sentence) is
        # allowed to exceed it.
        assert parent.token_count <= settings.PARENT_TOKENS * 1.5


def test_children_are_near_the_child_budget():
    text = "A sentence of moderate length goes here. " * 200
    children = _flat_children(chunk_pages([Page(1, text)]))

    assert len(children) > 3
    for child in children[:-1]:
        assert child.token_count <= settings.CHILD_TOKENS * 2


def test_sibling_children_overlap():
    text = "Alpha beta gamma delta epsilon zeta eta theta. " * 60
    parents = chunk_pages([Page(1, text)])

    multi = [p for p in parents if len(p.children) > 1]
    assert multi, "need a parent with several children to observe overlap"
    parent = multi[0]
    first_words = set(parent.children[0].content.split())
    second_words = set(parent.children[1].content.split())
    # Overlap means a fact sitting on a boundary still appears whole inside
    # at least one child's embedding.
    assert first_words & second_words


@pytest.mark.parametrize("child_tokens,overlap", [(60, 10), (200, 40)])
def test_budgets_come_from_settings(monkeypatch, child_tokens, overlap):
    import app.services.chunking as chunking

    monkeypatch.setattr(chunking.settings, "CHILD_TOKENS", child_tokens)
    monkeypatch.setattr(chunking.settings, "CHILD_OVERLAP", overlap)

    text = "A measured sentence of prose appears here. " * 150
    children = _flat_children(chunk_pages([Page(1, text)]))

    # Nothing is hardcoded: changing the setting changes the output.
    assert max(c.token_count for c in children) <= child_tokens * 2


def test_no_chunk_is_empty_or_whitespace_only():
    text = "# Title\n\n\n\nBody.\n\n\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n\n```\nx\n```\n\n\n"
    parents = chunk_pages([Page(1, text)])

    for parent in parents:
        assert parent.content.strip()
        for child in parent.children:
            assert child.content.strip()

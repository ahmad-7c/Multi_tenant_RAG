"""Cleaner tests.

PDF text extraction produces hard line breaks at the right margin, words
split across lines with hyphens, and a running header/footer on every
page. All three corrupt embeddings if left in: "manage-\nment" tokenizes
as two meaningless fragments, and a header repeated 40 times becomes the
most statistically prominent phrase in the document.
"""
from app.services.cleaning import clean_pages
from app.services.extraction import Page


def _clean_one(text: str) -> str:
    return clean_pages([Page(1, text)])[0].text


def test_single_newlines_inside_a_paragraph_are_joined():
    cleaned = _clean_one("This sentence was wrapped\nacross two lines by the PDF.")

    assert cleaned == "This sentence was wrapped across two lines by the PDF."


def test_double_newlines_are_preserved_as_paragraph_breaks():
    cleaned = _clean_one("First paragraph here.\n\nSecond paragraph here.")

    # The chunker splits on blank lines, so losing these would collapse the
    # whole page into one undifferentiated blob.
    assert cleaned == "First paragraph here.\n\nSecond paragraph here."


def test_three_or_more_newlines_collapse_to_two():
    cleaned = _clean_one("One.\n\n\n\nTwo.")

    assert cleaned == "One.\n\nTwo."


def test_hyphenated_line_break_is_rejoined():
    cleaned = _clean_one("The senior manage-\nment team approved it.")

    # Without this, the embedding sees "manage" and "ment" -- two tokens
    # that carry none of the meaning of "management".
    assert cleaned == "The senior management team approved it."


def test_genuine_hyphenated_compound_is_not_damaged():
    cleaned = _clean_one("A well-known risk applies here.")

    assert cleaned == "A well-known risk applies here."


def test_hyphen_before_a_capitalised_word_is_kept():
    # "Smith-\nJones" is far more likely a real hyphenated name split by
    # the margin than a word break, so the hyphen is preserved while the
    # newline is still removed.
    cleaned = _clean_one("Reported by Smith-\nJones last quarter.")

    assert cleaned == "Reported by Smith-Jones last quarter."


def test_running_header_repeated_on_every_page_is_removed():
    pages = [
        Page(1, "ACME INTERNAL CONFIDENTIAL\n\nFirst page body text."),
        Page(2, "ACME INTERNAL CONFIDENTIAL\n\nSecond page body text."),
        Page(3, "ACME INTERNAL CONFIDENTIAL\n\nThird page body text."),
        Page(4, "ACME INTERNAL CONFIDENTIAL\n\nFourth page body text."),
    ]

    cleaned = clean_pages(pages)

    for page in cleaned:
        assert "CONFIDENTIAL" not in page.text
    assert "First page body text." in cleaned[0].text


def test_running_footer_with_changing_page_numbers_is_removed():
    # A realistic page: a body that genuinely differs page to page, then
    # the footer. Differing *words* -- not just a differing number -- are
    # what distinguish content from furniture, because the comparison key
    # normalizes digits away.
    bodies = [
        "The operating model assigns ownership to each regional lead.",
        "Procurement thresholds require two approvals above that limit.",
        "Escalation paths route through the duty manager overnight.",
        "Tooling budgets are reviewed alongside headcount planning.",
        "Retention figures are reported to the board every quarter.",
    ]
    pages = [Page(i + 1, f"{body}\n\nPage {i + 1} of 5") for i, body in enumerate(bodies)]

    cleaned = clean_pages(pages)

    # The literal footer text differs on every page, so matching has to
    # normalize digits before comparing -- otherwise the single most common
    # footer format in existence is never detected.
    for page in cleaned:
        assert "of 5" not in page.text
    assert "regional lead" in cleaned[0].text


def test_prose_containing_numbers_is_not_mistaken_for_a_footer():
    """Digit normalization is dangerous without a length cap.

    "Revenue grew 12 percent..." and "Revenue grew 18 percent..." normalize
    to the same key, so without the cap a real sentence repeated across
    pages would be silently deleted as a running footer.
    """
    pages = [
        Page(
            i,
            f"Revenue grew {10 + i} percent year over year across every "
            f"reported segment, which the board reviewed in detail.\n"
            f"\nPage {i} of 5",
        )
        for i in range(1, 6)
    ]

    cleaned = clean_pages(pages)

    assert "Revenue grew 11 percent" in cleaned[0].text
    assert "of 5" not in cleaned[0].text


def test_a_page_is_never_stripped_empty():
    # If the heuristic matches every line on a page it is wrong about that
    # page; losing the content outright is far worse than a stray header.
    pages = [Page(i, "Confidential") for i in range(1, 6)]

    cleaned = clean_pages(pages)

    assert all(page.text.strip() for page in cleaned)


def test_a_line_appearing_on_only_some_pages_is_kept():
    pages = [
        Page(1, "Shared heading\n\nBody one."),
        Page(2, "Shared heading\n\nBody two."),
        Page(3, "Different content entirely.\n\nBody three."),
        Page(4, "Another unique line.\n\nBody four."),
        Page(5, "And another.\n\nBody five."),
    ]

    cleaned = clean_pages(pages)

    # 2 of 5 pages is below the repeat ratio: a heading that happens to
    # recur twice is content, not furniture.
    assert "Shared heading" in cleaned[0].text


def test_header_detection_is_skipped_for_short_documents():
    pages = [Page(1, "Title line\n\nBody one."), Page(2, "Title line\n\nBody two.")]

    cleaned = clean_pages(pages)

    # With two pages there is no way to distinguish a running header from a
    # line that legitimately appears on both.
    assert "Title line" in cleaned[0].text


def test_fenced_code_block_keeps_its_line_structure():
    text = "Intro paragraph.\n\n```python\ndef f():\n    return 1\n```\n\nAfter."

    cleaned = _clean_one(text)

    # Joining single newlines inside a code fence would turn the code into
    # one unrunnable line -- and the chunker relies on fences staying
    # recognisable to keep them whole.
    assert "def f():\n    return 1" in cleaned


def test_markdown_table_keeps_one_row_per_line():
    text = "| Region | Revenue |\n| --- | --- |\n| EMEA | 12 |\n| APAC | 9 |"

    cleaned = _clean_one(text)

    assert cleaned.count("\n") == 3
    assert "| EMEA | 12 |" in cleaned


def test_markdown_heading_stays_on_its_own_line():
    cleaned = _clean_one("# Section Title\nFirst body line.")

    # A heading absorbed into the following paragraph stops being a
    # structural boundary the chunker can split on.
    assert cleaned.startswith("# Section Title\n")
    assert "First body line." in cleaned


def test_list_items_are_not_joined_together():
    cleaned = _clean_one("- first item\n- second item\n- third item")

    assert cleaned.count("\n") == 2


def test_page_numbers_and_count_are_preserved():
    pages = [Page(1, "a" * 40), Page(2, "b" * 40), Page(3, "c" * 40)]

    cleaned = clean_pages(pages)

    assert [p.page_number for p in cleaned] == [1, 2, 3]


def test_trailing_and_leading_whitespace_is_stripped():
    assert _clean_one("   \n\n  Body text here.  \n\n   ") == "Body text here."


def test_windows_line_endings_are_normalised():
    cleaned = _clean_one("Wrapped line\r\ncontinues here.\r\n\r\nNew paragraph.")

    assert "\r" not in cleaned
    assert cleaned == "Wrapped line continues here.\n\nNew paragraph."

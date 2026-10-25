"""Citation validator tests."""
import uuid

from app.services.citations import validate_citations
from app.services.prompting import ContextBlock


def _block(marker: int, *, filename="handbook.pdf", page=1, content="Body text."):
    return ContextBlock(
        marker=marker,
        parent_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename=filename,
        page_number=page,
        content=content,
    )


def test_valid_marker_produces_a_citation():
    blocks = [_block(1, filename="policy.pdf", page=4, content="Thirty days.")]

    answer, citations = validate_citations("Receipts take thirty days [1].", blocks)

    assert answer == "Receipts take thirty days [1]."
    assert len(citations) == 1
    assert citations[0].document == "policy.pdf"
    assert citations[0].page == 4
    assert citations[0].snippet == "Thirty days."


def test_marker_not_in_the_supplied_set_is_dropped():
    blocks = [_block(1)]

    answer, citations = validate_citations("A claim [1] and another [7].", blocks)

    # A visible [7] that resolves to nothing is worse than no marker --
    # the reader assumes there is a source behind it.
    assert "[7]" not in answer
    assert [c.marker for c in citations] == [1]


def test_all_markers_invalid_leaves_no_citations():
    answer, citations = validate_citations("Totally unsupported [3][4].", [_block(1)])

    assert citations == []
    assert "[3]" not in answer and "[4]" not in answer


def test_multiple_markers_in_one_bracket_group():
    blocks = [_block(1), _block(2), _block(3)]

    answer, citations = validate_citations("Claim [1][3].", blocks)

    assert [c.marker for c in citations] == [1, 3]
    assert "[1][3]" in answer


def test_comma_separated_markers_are_parsed():
    blocks = [_block(1), _block(2)]

    _, citations = validate_citations("Claim [1, 2].", blocks)

    assert [c.marker for c in citations] == [1, 2]


def test_mixed_valid_and_invalid_inside_one_group():
    blocks = [_block(2)]

    answer, citations = validate_citations("Claim [2, 9].", blocks)

    assert [c.marker for c in citations] == [2]
    assert "[2]" in answer
    assert "9" not in answer


def test_repeated_marker_yields_one_citation():
    blocks = [_block(1)]

    _, citations = validate_citations("One [1]. Two [1]. Three [1].", blocks)

    assert len(citations) == 1


def test_citations_are_ordered_by_first_appearance():
    blocks = [_block(1), _block(2), _block(3)]

    _, citations = validate_citations("First [3]. Then [1]. Then [2].", blocks)

    # The order a reader encounters them.
    assert [c.marker for c in citations] == [3, 1, 2]


def test_answer_with_no_markers_yields_no_citations():
    answer, citations = validate_citations("I don't have enough information.", [_block(1)])

    assert citations == []
    assert answer == "I don't have enough information."


def test_dropping_a_marker_does_not_leave_a_dangling_space():
    answer, _ = validate_citations("A claim [9].", [_block(1)])

    assert answer == "A claim."


def test_citation_resolves_to_the_server_side_chunk_not_model_text():
    """The model cannot author provenance.

    Even when the answer text names a different document, the citation
    comes from the block the server supplied.
    """
    blocks = [_block(1, filename="real.pdf", page=2)]

    _, citations = validate_citations(
        "According to totally-made-up.pdf page 99, the answer is yes [1].", blocks
    )

    assert citations[0].document == "real.pdf"
    assert citations[0].page == 2


def test_citation_carries_ids_for_the_client_to_link_to():
    block = _block(1)

    _, citations = validate_citations("Claim [1].", [block])

    assert citations[0].document_id == block.document_id
    assert citations[0].chunk_id == block.parent_id


def test_long_snippet_is_truncated_at_a_word_boundary():
    content = "word " * 200
    _, citations = validate_citations("Claim [1].", [_block(1, content=content)])

    snippet = citations[0].snippet
    assert len(snippet) <= 320
    assert snippet.endswith("...")
    assert "wor..." not in snippet


def test_snippet_collapses_whitespace():
    _, citations = validate_citations(
        "Claim [1].", [_block(1, content="line one\n\n   line two\t\tline three")]
    )

    assert citations[0].snippet == "line one line two line three"


def test_zero_marker_is_dropped():
    # Markers are 1-indexed; [0] can only be a model error.
    answer, citations = validate_citations("Claim [0].", [_block(1)])

    assert citations == []
    assert "[0]" not in answer


def test_validation_is_pure_text_and_does_not_need_a_database():
    # No db fixture requested anywhere in this module: validation works off
    # the block list alone, which is what lets it run after the DB session
    # has already been closed for streaming.
    _, citations = validate_citations("Claim [1].", [_block(1)])
    assert citations

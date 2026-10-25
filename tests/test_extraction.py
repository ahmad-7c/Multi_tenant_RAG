"""Extractor tests.

The extractor is the only place that touches file formats, and the only
place that can detect "this document has no text in it" -- which is the
single most common real-world ingestion failure (someone uploads a scan).
"""
import pymupdf
import pytest

from app.services.extraction import (
    EmptyDocumentError,
    NeedsOCRError,
    UnsupportedFormatError,
    extract_pages,
)


def _pdf_with_pages(texts: list[str]) -> bytes:
    doc = pymupdf.open()
    for body in texts:
        page = doc.new_page()
        page.insert_text((72, 100), body, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _image_only_pdf(page_count: int = 2) -> bytes:
    """A PDF with pages but no text layer -- what a scanner produces."""
    doc = pymupdf.open()
    for _ in range(page_count):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_pdf_pages_are_extracted_with_one_indexed_page_numbers():
    data = _pdf_with_pages(
        [
            "First page about quarterly revenue and the finance team.",
            "Second page about engineering headcount and hiring plans.",
            "Third page about customer support escalation policy.",
        ]
    )

    pages = extract_pages(data, "report.pdf")

    assert [p.page_number for p in pages] == [1, 2, 3]
    # 1-indexed because page numbers end up in user-facing citations, and
    # "page 0" is not a thing a human recognizes.
    assert "quarterly revenue" in pages[0].text
    assert "engineering headcount" in pages[1].text


def test_txt_is_a_single_synthetic_page():
    pages = extract_pages(b"plain text body with enough words to count", "notes.txt")

    # .txt has no notion of a page. Faking pagination would put a page
    # number in a citation that the reader cannot verify.
    assert len(pages) == 1
    assert pages[0].page_number == 1


def test_md_is_extracted_as_text():
    pages = extract_pages(b"# Heading\n\nSome markdown body text here.", "notes.md")

    assert len(pages) == 1
    assert "# Heading" in pages[0].text


def test_scanned_pdf_fails_with_a_needs_ocr_error():
    data = _image_only_pdf(page_count=3)

    with pytest.raises(NeedsOCRError) as exc:
        extract_pages(data, "scan.pdf")

    # The message has to name OCR: this lands in documents.error_message
    # and is the only thing the uploader will see. "extraction failed"
    # would leave them with no idea what to do next.
    assert "OCR" in str(exc.value)


def test_empty_text_file_fails_with_a_clear_error():
    with pytest.raises(EmptyDocumentError):
        extract_pages(b"   \n\n  \t ", "empty.txt")


def test_unsupported_extension_is_rejected():
    with pytest.raises(UnsupportedFormatError):
        extract_pages(b"MZ\x90\x00", "payload.exe")


def test_corrupt_pdf_raises_an_extraction_error_not_a_crash():
    # Starts with the PDF magic bytes but is not a PDF. Must surface as one
    # of our own exception types so the task can fail the document cleanly.
    with pytest.raises((NeedsOCRError, EmptyDocumentError, UnsupportedFormatError)):
        extract_pages(b"%PDF-1.4 this is not actually a pdf", "broken.pdf")


def test_sparse_but_real_pdf_is_accepted():
    # A title page plus a real page: mean chars-per-page is low but the
    # document genuinely has a text layer, so it must not be rejected as a
    # scan.
    data = _pdf_with_pages(
        ["", "A genuine paragraph of extracted text that carries real content."]
    )

    pages = extract_pages(data, "title-then-body.pdf")

    assert len(pages) == 2


def test_extension_matching_is_case_insensitive():
    pages = extract_pages(b"body text that is long enough", "NOTES.TXT")
    assert len(pages) == 1

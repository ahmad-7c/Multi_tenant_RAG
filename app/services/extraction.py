"""Bytes -> pages of text.

The only module that knows about file formats. Everything downstream works
on `Page` objects and never asks what the original file was.
"""
import logging
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.core.config import settings

logger = logging.getLogger(__name__)

PDF_EXTENSIONS = {".pdf"}
TEXT_EXTENSIONS = {".txt", ".md"}
SUPPORTED_EXTENSIONS = PDF_EXTENSIONS | TEXT_EXTENSIONS


class ExtractionError(Exception):
    """Base for every unrecoverable problem with the input file.

    All of these are permanent: the same bytes will fail the same way, so
    the task must fail the document rather than spend retries on it.
    """


class UnsupportedFormatError(ExtractionError):
    pass


class EmptyDocumentError(ExtractionError):
    pass


class NeedsOCRError(ExtractionError):
    pass


@dataclass(frozen=True)
class Page:
    # 1-indexed: page numbers end up in user-facing citations, and "page 0"
    # is not something a reader can act on.
    page_number: int
    text: str


def _extract_pdf(data: bytes) -> list[Page]:
    try:
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            pages = [Page(i + 1, page.get_text() or "") for i, page in enumerate(pdf)]
    except Exception as exc:
        # PyMuPDF raises a variety of its own types for a truncated or
        # malformed file. Collapse them into our own error so the caller
        # has exactly one exception family to handle.
        raise UnsupportedFormatError(f"Could not read the PDF: {exc}") from exc

    if not pages:
        raise EmptyDocumentError("The PDF contains no pages.")

    # A scanned document has pages but no text layer, so extraction
    # "succeeds" and returns empty strings. Checked on the MEAN rather than
    # per page: a real document legitimately has near-empty pages (title
    # pages, full-page figures, section dividers), and failing on any one
    # of them would reject documents that are perfectly fine.
    total_chars = sum(len("".join(p.text.split())) for p in pages)
    if total_chars < settings.OCR_MIN_CHARS_PER_PAGE * len(pages):
        raise NeedsOCRError(
            f"No extractable text layer was found ({total_chars} characters across "
            f"{len(pages)} pages). This looks like a scanned document -- it needs "
            f"OCR before it can be ingested."
        )

    return pages


def _extract_text(data: bytes) -> list[Page]:
    # errors="replace" rather than strict: one bad byte in a 200-page text
    # file should not fail the whole document.
    text = data.decode("utf-8", errors="replace")
    if not text.strip():
        raise EmptyDocumentError("The file is empty.")
    # .txt/.md have no notion of a page. One synthetic page is honest;
    # inventing page breaks would put numbers in citations that the reader
    # cannot verify against the source.
    return [Page(1, text)]


def extract_pages(data: bytes, filename: str) -> list[Page]:
    suffix = Path(filename or "").suffix.lower()

    if suffix in PDF_EXTENSIONS:
        return _extract_pdf(data)
    if suffix in TEXT_EXTENSIONS:
        return _extract_text(data)

    # Reached only if the upload endpoint's allowlist and this one drift
    # apart; raising here keeps the worker from guessing at a format.
    raise UnsupportedFormatError(
        f"Unsupported file type '{suffix}'. Supported: "
        f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}"
    )

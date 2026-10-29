"""Page-aware PDF extraction for the resumable bank-statement parser.

Extraction is deliberately lossless at this boundary: repeated headers, wrapped
descriptions, duplicate rows, and cross-page continuation markers remain exactly as
``pdfplumber`` returns them.  Interpretation belongs to the deterministic parser.
Image-only pages are honest evidence, not an error and not fabricated OCR output.
"""
from __future__ import annotations

import hashlib
import io

import pdfplumber

from .parser_types import (
    EXTRACTOR_VERSION,
    ExtractedPage,
    ExtractedPdf,
    ExtractedTable,
    ExtractedWord,
    PdfTextStatus,
)


class PdfExtractionError(ValueError):
    """The supplied bytes could not be decoded completely as a PDF."""


def _text_status(pages: tuple[ExtractedPage, ...]) -> PdfTextStatus:
    has_text = [bool(page.source_text.strip()) for page in pages]
    if has_text and all(has_text):
        return PdfTextStatus.TEXT
    if any(has_text):
        return PdfTextStatus.MIXED
    if any(page.image_count > 0 for page in pages):
        return PdfTextStatus.IMAGE_ONLY
    return PdfTextStatus.EMPTY


def _extract_words(page: pdfplumber.page.Page) -> tuple[ExtractedWord, ...]:
    return tuple(
        ExtractedWord(
            text=str(word["text"]),
            x0=float(word["x0"]),
            top=float(word["top"]),
            x1=float(word["x1"]),
            bottom=float(word["bottom"]),
        )
        for word in page.extract_words()
    )


def _extract_tables(page: pdfplumber.page.Page) -> tuple[ExtractedTable, ...]:
    tables: list[ExtractedTable] = []
    for table in page.find_tables():
        extracted_rows = table.extract() or []
        tables.append(
            ExtractedTable(
                bbox=tuple(float(value) for value in table.bbox),
                rows=tuple(tuple(cell for cell in row) for row in extracted_rows),
            )
        )
    return tuple(tables)


def extract_pdf(file_bytes: bytes) -> ExtractedPdf:
    """Extract every PDF page without a byte, page, character, or row ceiling.

    Canonical document text is the direct concatenation of exact per-page extraction
    results.  No separator is injected: this preserves the frozen corpus character
    counts.  Page numbers make zero-width/image pages and cross-page spans explicit.
    """
    if not isinstance(file_bytes, bytes):
        raise TypeError("file_bytes must be bytes")

    source_sha256 = hashlib.sha256(file_bytes).hexdigest()
    pages: list[ExtractedPage] = []
    source_parts: list[str] = []
    cursor = 0

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as document:
            for page_number, page in enumerate(document.pages, start=1):
                source_text = page.extract_text() or ""
                span_start = cursor
                span_end = span_start + len(source_text)
                pages.append(
                    ExtractedPage(
                        page_number=page_number,
                        span_start=span_start,
                        span_end=span_end,
                        source_text=source_text,
                        width=float(page.width),
                        height=float(page.height),
                        rotation=int(page.rotation or 0),
                        image_count=len(page.images),
                        words=_extract_words(page),
                        tables=_extract_tables(page),
                    )
                )
                source_parts.append(source_text)
                cursor = span_end
    except Exception as exc:
        raise PdfExtractionError("Could not extract the supplied PDF.") from exc

    frozen_pages = tuple(pages)
    source_text = "".join(source_parts)
    return ExtractedPdf(
        source_sha256=source_sha256,
        source_size_bytes=len(file_bytes),
        extractor_version=EXTRACTOR_VERSION,
        text_status=_text_status(frozen_pages),
        source_text=source_text,
        pages=frozen_pages,
    )


class PdfStatementExtractor:
    """Concrete adapter for the frozen :class:`StatementExtractor` protocol."""

    def extract(self, source_bytes: bytes) -> ExtractedPdf:
        return extract_pdf(source_bytes)


__all__ = ["EXTRACTOR_VERSION", "PdfExtractionError", "PdfStatementExtractor", "extract_pdf"]

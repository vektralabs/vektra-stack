"""PDF text extraction with pdfminer.six (COMP-008).

pdfplumber is a wrapper over pdfminer; using pdfminer directly drops the wrapper's
imported surface and address-space footprint. Encrypted files are refused, not opened.
"""

from __future__ import annotations

import io
from collections.abc import Iterator

from pdfminer.converter import TextConverter
from pdfminer.layout import LAParams
from pdfminer.pdfdocument import PDFDocument, PDFPasswordIncorrect
from pdfminer.pdfexceptions import PDFException
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import resolve1

from vektra_assess.parser_protocol import Extracted, FileStatus
from vektra_assess.parser_sidecar.text import clean

# Scanned-PDF heuristic. NOT the ingest one (avg < 100 chars/page): ingest uses that figure to
# decide on OCR, while REQ-101 reads any "PDF with a text layer", and a one-line PDF has one. A PDF
# is "testo non estraibile" here only when the sampled pages carry almost no visible characters
# (found on the plan 4 recordings: a real one-line text PDF was reported as scanned).
_SCAN_THRESHOLD = (
    5  # avg NON-WHITESPACE chars per sampled page below which it is scanned
)
_SCAN_SAMPLE_PAGES = 5  # pages to sample for scanned detection


def _declared_pages(doc: PDFDocument) -> int | None:
    try:
        count = resolve1(resolve1(doc.catalog["Pages"])["Count"])
    except Exception:
        return None
    return count if isinstance(count, int) and count >= 0 else None


def _pages(doc: PDFDocument) -> Iterator[PDFPage]:
    return PDFPage.create_pages(doc)


def extract_pdf(data: bytes, max_pages: int, max_chars: int) -> Extracted:
    try:
        parser = PDFParser(io.BytesIO(data))
        doc = PDFDocument(parser)
    except PDFPasswordIncorrect:
        return Extracted(FileStatus.PROTECTED, code="pdf_encrypted")
    except PDFException:
        return Extracted(FileStatus.CORRUPT, code="pdf_unreadable")
    if doc.encryption is not None:
        return Extracted(FileStatus.PROTECTED, code="pdf_encrypted")
    pages_total = _declared_pages(doc)

    rsrc = PDFResourceManager()
    texts: list[str] = []
    page_no = 0
    truncated_pages = False
    try:
        it = _pages(doc)
        for page in it:
            if page_no >= max_pages:
                truncated_pages = True
                break
            page_no += 1
            out = io.StringIO()
            conv = TextConverter(rsrc, out, laparams=LAParams())
            PDFPageInterpreter(rsrc, conv).process_page(page)
            conv.close()
            texts.append(clean(out.getvalue()).strip())
    except PDFPasswordIncorrect:
        return Extracted(FileStatus.PROTECTED, code="pdf_encrypted")
    except PDFException:
        return Extracted(FileStatus.CORRUPT, code="pdf_parse_error")
    if page_no == 0:
        return Extracted(FileStatus.CORRUPT, code="pdf_no_pages")

    sample = texts[:_SCAN_SAMPLE_PAGES]
    visible = sum(len("".join(t.split())) for t in sample)
    if visible / len(sample) < _SCAN_THRESHOLD:
        return Extracted(
            FileStatus.NO_TEXT, pages_total=pages_total, code="pdf_scanned"
        )

    out_parts: list[str] = []
    used = 0
    for i, t in enumerate(texts, 1):
        add = len(t) + (2 if out_parts else 0)
        if used + add > max_chars:
            if not out_parts:  # a single page above the cap: keep its head
                return Extracted(
                    FileStatus.READ_PARTIAL,
                    t[:max_chars],
                    upto_page=i,
                    pages_total=pages_total,
                    code="char_cap",
                )
            return Extracted(
                FileStatus.READ_PARTIAL,
                "\n\n".join(out_parts),
                upto_page=i - 1,
                pages_total=pages_total,
                code="char_cap",
            )
        out_parts.append(t)
        used += add
    text = "\n\n".join(out_parts)
    if truncated_pages:
        return Extracted(
            FileStatus.READ_PARTIAL,
            text,
            upto_page=page_no,
            pages_total=pages_total,
            code="page_cap",
        )
    return Extracted(FileStatus.READ, text, pages_total=pages_total or page_no)

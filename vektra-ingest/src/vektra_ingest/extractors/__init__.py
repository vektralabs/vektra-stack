"""Document extractor implementations for vektra-ingest.

Phase 1 extractors:
- PdfplumberExtractor: PDF text extraction with scanned detection
- WordExtractor: DOCX paragraph/heading extraction
- PowerPointExtractor: PPTX slide text and speaker notes extraction
"""
from __future__ import annotations

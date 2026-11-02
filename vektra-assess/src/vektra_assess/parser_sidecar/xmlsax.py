"""Hardened streaming XML: defusedxml expat reader, DTDs, entities and external refs refused.

SAX (not a tree) so memory stays flat on a large ``document.xml`` and the walk can stop
at the character cap.
"""

from __future__ import annotations

import io
import xml.sax
import xml.sax.handler
from typing import Any

from defusedxml import DefusedXmlException
from defusedxml.expatreader import create_parser

from vektra_assess.parser_protocol import Extracted, FileStatus
from vektra_assess.parser_sidecar.text import clean
from vektra_assess.parser_sidecar.zipguard import ZipRefused


class Stop(Exception):
    """Raised by a handler once the character cap is reached."""


class XmlRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def run_sax(stream: io.BufferedIOBase, handler: xml.sax.handler.ContentHandler) -> bool:
    """Parse ``stream`` into ``handler``. True when the handler stopped the walk early."""
    parser = create_parser(forbid_dtd=True, forbid_entities=True, forbid_external=True)
    parser.setFeature(xml.sax.handler.feature_namespaces, True)
    parser.setContentHandler(handler)
    try:
        parser.parse(stream)
    except Stop:
        return True
    except DefusedXmlException:
        raise XmlRefused("xml_forbidden_construct") from None
    except xml.sax.SAXException:
        raise XmlRefused("xml_malformed") from None
    return False


class TextSink:
    """Accumulates blocks and enforces the character cap."""

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self.blocks: list[str] = []
        self.size = 0

    def add(self, block: str) -> None:
        if not block:
            return
        self.blocks.append(block)
        self.size += len(block) + 1
        if self.size > self.max_chars:
            raise Stop

    def result(self, stopped: bool) -> Extracted:
        text = clean("\n".join(self.blocks))
        if stopped or len(text) > self.max_chars:
            return Extracted(
                FileStatus.READ_PARTIAL, text[: self.max_chars], code="char_cap"
            )
        if not text.strip():
            return Extracted(FileStatus.NO_TEXT, code="empty")
        return Extracted(FileStatus.READ, text)


def guarded(fn: Any) -> Extracted:
    """Run an extractor body, mapping the guard exceptions to CORRUPT."""
    try:
        res: Extracted = fn()
        return res
    except ZipRefused as exc:
        return Extracted(FileStatus.CORRUPT, code=exc.code)
    except XmlRefused as exc:
        return Extracted(FileStatus.CORRUPT, code=exc.code)

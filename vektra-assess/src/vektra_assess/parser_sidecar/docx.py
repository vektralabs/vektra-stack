"""DOCX text in body order: zipfile + defusedxml SAX over ``word/document.xml``.

python-docx is not used (lxml on the hostile path, paragraphs only, no DTD hardening).
Relationships are never read, so a remote ``attachedTemplate`` is never resolved.
Macro-enabled files are refused by ``sniff`` before this runs.
"""

from __future__ import annotations

import re
from typing import Any

from vektra_assess.parser_protocol import Extracted, FileStatus
from vektra_assess.parser_sidecar.xmlsax import (
    CappedHandler,
    TextSink,
    guarded,
    run_sax,
)
from vektra_assess.parser_sidecar.zipguard import open_checked

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
IMAGE_PLACEHOLDER = "[immagine non visibile all'assistente]"
MAX_PART_BYTES = 32 * 1024 * 1024
_HEADING = re.compile(r"^(?:heading|titolo|intestazione)\s*(\d)$", re.IGNORECASE)


class _Handler(CappedHandler):
    def __init__(self, sink: TextSink) -> None:
        super().__init__(sink)
        # a stack: a text box (w:txbxContent) holds paragraphs inside a paragraph
        self.paras: list[list[str]] = []
        self.headings: list[int] = []
        self.in_t = False
        self.math_depth = 0
        self.math: list[str] = []
        self.in_mt = False
        self.tables: list[dict[str, Any]] = []

    def _emit(self, text: str) -> None:
        if self.tables:
            self.tables[-1]["cell"].append(text)
        else:
            self.sink.add(text)

    def flush_open(self) -> None:
        for t in reversed(self.tables):
            self.sink.force(" ".join(t["cell"]))
        for para in reversed(self.paras):
            self.sink.force("".join(para))
        if self.math_depth:
            self.sink.force("".join(self.math))

    def startElementNS(self, name: Any, qname: Any, attrs: Any) -> None:
        ns, local = name
        if ns == W:
            if local == "p":
                self.paras.append([])
                self.headings.append(0)
            elif local == "pStyle" and self.headings:
                m = _HEADING.match(attrs.get((W, "val"), ""))
                if m:
                    self.headings[-1] = max(1, min(6, int(m.group(1))))
            elif local == "t":
                self.in_t = True
            elif local == "tab" and self.paras:
                self.paras[-1].append("\t")
            elif local in ("br", "cr") and self.paras:
                self.paras[-1].append("\n")
            elif local == "tbl":
                self.tables.append({"row": [], "cell": [], "rows": []})
            elif local == "tr" and self.tables:
                self.tables[-1]["row"] = []
            elif local == "tc" and self.tables:
                self.tables[-1]["cell"] = []
        elif ns == M:
            if local == "oMath":
                self.math_depth += 1
                if self.math_depth == 1:
                    self.math = []
            elif local == "t":
                self.in_mt = True
        elif ns == WP and local == "docPr" and self.paras:
            alt = (
                attrs.get((None, "descr")) or attrs.get((None, "title")) or ""
            ).strip()
            self.paras[-1].append(f"[immagine: {alt}]" if alt else IMAGE_PLACEHOLDER)

    def characters(self, content: str) -> None:
        if self.in_t and self.paras:
            self.paras[-1].append(content)
            self.count(content)
        elif self.in_mt:
            self.math.append(content)
            self.count(content)

    def endElementNS(self, name: Any, qname: Any) -> None:
        ns, local = name
        if ns == W:
            if local == "t":
                self.in_t = False
            elif local == "p" and self.paras:
                text = "".join(self.paras.pop())
                heading = self.headings.pop()
                if text.strip():
                    prefix = "#" * heading + " " if heading else ""
                    self._emit(prefix + text)
            elif local == "tc" and self.tables:
                t = self.tables[-1]
                t["row"].append(" ".join(t["cell"]))
                t["cell"] = []
            elif local == "tr" and self.tables:
                t = self.tables[-1]
                line = "| " + " | ".join(t["row"]) + " |"
                t["row"] = []
                if len(self.tables) >= 2:
                    self.tables[-2]["cell"].append(line)
                else:
                    self.sink.add(line)
            elif local == "tbl" and self.tables:
                self.tables.pop()
        elif ns == M:
            if local == "t":
                self.in_mt = False
            elif local == "oMath" and self.math_depth:
                self.math_depth -= 1
                if self.math_depth == 0 and self.paras:
                    self.paras[-1].append("".join(self.math))


def extract_docx(data: bytes, max_chars: int) -> Extracted:
    def run() -> Extracted:
        gz = open_checked(data)
        if gz.is_encrypted():
            return Extracted(FileStatus.PROTECTED, code="zip_encrypted")
        if "word/document.xml" not in gz.names:
            return Extracted(FileStatus.CORRUPT, code="docx_no_body")
        sink = TextSink(max_chars)
        with gz.open("word/document.xml", MAX_PART_BYTES) as stream:
            stopped = run_sax(stream, _Handler(sink))
        return sink.result(stopped)

    return guarded(run)

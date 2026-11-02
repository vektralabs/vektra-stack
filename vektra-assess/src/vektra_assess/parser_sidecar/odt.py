"""ODT text: zipfile + defusedxml SAX over ``content.xml`` (REQ-101).

Encrypted documents (``manifest:encryption-data``) are PROTECTED. Embedded formula
objects yield their TeX annotation, else the MathML source as text. Object references
are looked up inside the archive only; nothing external is ever followed.
"""

from __future__ import annotations

import re
import xml.sax.handler
from typing import Any

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

from vektra_assess.parser_protocol import Extracted, FileStatus
from vektra_assess.parser_sidecar.text import serialize_xml
from vektra_assess.parser_sidecar.xmlsax import (
    CappedHandler,
    TextSink,
    guarded,
    run_sax,
)
from vektra_assess.parser_sidecar.zipguard import GuardedZip, open_checked

T = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TB = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
D = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
SVG = "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
XLINK = "http://www.w3.org/1999/xlink"
MF = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
IMAGE_PLACEHOLDER = "[immagine non visibile all'assistente]"
MAX_PART_BYTES = 32 * 1024 * 1024
MAX_FORMULA_CHARS = 8000
_OBJECT_REF = re.compile(r"^(?:\./)?([^/\\:.][^/\\:]*)$")


class _ManifestHandler(xml.sax.handler.ContentHandler):
    encrypted = False

    def startElementNS(self, name: Any, qname: Any, attrs: Any) -> None:
        if name == (MF, "encryption-data"):
            self.encrypted = True


def _formula(gz: GuardedZip, href: str) -> str | None:
    m = _OBJECT_REF.match(href)
    if not m:
        return None
    part = f"{m.group(1)}/content.xml"
    if part not in gz.names:
        return None
    try:
        root = fromstring(gz.read(part, 1 << 20), forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError):
        return None
    for el in root.iter():
        if (
            el.tag.endswith("}annotation")
            and "tex" in (el.get("encoding") or "").lower()
        ):
            return "".join(el.itertext())
    return serialize_xml(root)[:MAX_FORMULA_CHARS]


class _Handler(CappedHandler):
    def __init__(self, sink: TextSink, gz: GuardedZip) -> None:
        super().__init__(sink)
        self.gz = gz
        self.paras: list[list[str]] = []
        self.heading = 0
        self.list_depth = 0
        self.fresh_item = False
        self.tables: list[dict[str, Any]] = []
        self.frames: list[dict[str, Any]] = []
        self.in_alt = False
        self.alt: list[str] = []

    def flush_open(self) -> None:
        for t in reversed(self.tables):
            self.sink.force(" ".join(t["cell"]))
        for para in reversed(self.paras):
            self.sink.force("".join(para))

    def _emit(self, text: str) -> None:
        if self.tables:
            self.tables[-1]["cell"].append(text)
        else:
            self.sink.add(text)

    def _inline(self, text: str) -> None:
        if self.paras:
            self.paras[-1].append(text)
        else:
            self._emit(text)

    def startElementNS(self, name: Any, qname: Any, attrs: Any) -> None:
        ns, local = name
        if ns == T:
            if local in ("p", "h"):
                self.paras.append([])
                self.heading = (
                    int(attrs.get((T, "outline-level"), "1") or 1)
                    if local == "h"
                    else 0
                )
                if self.heading < 0:
                    self.heading = 1
            elif local == "list":
                self.list_depth += 1
            elif local == "list-item":
                self.fresh_item = True
            elif local == "s":
                self._inline(" " * min(int(attrs.get((T, "c"), "1") or 1), 80))
            elif local == "tab":
                self._inline("\t")
            elif local == "line-break":
                self._inline("\n")
        elif ns == TB:
            if local == "table":
                self.tables.append({"row": [], "cell": []})
            elif local == "table-row" and self.tables:
                self.tables[-1]["row"] = []
            elif local == "table-cell" and self.tables:
                self.tables[-1]["cell"] = []
        elif ns == D:
            if local == "frame":
                self.frames.append({"obj": None, "image": False, "box": False})
            elif self.frames and local == "object":
                self.frames[-1]["obj"] = attrs.get((XLINK, "href"), "")
            elif self.frames and local == "image":
                self.frames[-1]["image"] = True
            elif self.frames and local == "text-box":
                self.frames[-1]["box"] = True
        elif ns == SVG and local in ("title", "desc") and self.frames:
            self.in_alt = True

    def characters(self, content: str) -> None:
        if self.in_alt:
            self.alt.append(content)
            self.count(content)
        elif self.paras:
            self.paras[-1].append(content)
            self.count(content)

    def endElementNS(self, name: Any, qname: Any) -> None:
        ns, local = name
        if ns == T:
            if local in ("p", "h") and self.paras:
                text = "".join(self.paras.pop())
                if text.strip():
                    prefix = ""
                    if local == "h":
                        prefix = "#" * max(1, min(self.heading, 6)) + " "
                    elif self.list_depth and self.fresh_item:
                        prefix = "  " * (self.list_depth - 1) + "- "
                    self.fresh_item = False
                    self._emit(prefix + text)
            elif local == "list" and self.list_depth:
                self.list_depth -= 1
        elif ns == TB and self.tables:
            t = self.tables[-1]
            if local == "table-cell":
                t["row"].append(" ".join(t["cell"]))
                t["cell"] = []
            elif local == "table-row":
                line = "| " + " | ".join(t["row"]) + " |"
                t["row"] = []
                if len(self.tables) >= 2:
                    self.tables[-2]["cell"].append(line)
                else:
                    self.sink.add(line)
            elif local == "table":
                self.tables.pop()
        elif ns == SVG and local in ("title", "desc"):
            self.in_alt = False
        elif ns == D and local == "frame" and self.frames:
            fr = self.frames.pop()
            alt = " ".join("".join(self.alt).split())
            self.alt = []
            if fr["obj"] is not None:
                f = _formula(self.gz, fr["obj"])
                if f:
                    self._inline(f)
            elif fr["image"] or not fr["box"]:
                self._inline(f"[immagine: {alt}]" if alt else IMAGE_PLACEHOLDER)


def extract_odt(data: bytes, max_chars: int) -> Extracted:
    def run() -> Extracted:
        gz = open_checked(data)
        if gz.is_encrypted():
            return Extracted(FileStatus.PROTECTED, code="zip_encrypted")
        if "META-INF/manifest.xml" in gz.names:
            mh = _ManifestHandler()
            with gz.open("META-INF/manifest.xml", 1 << 20) as stream:
                run_sax(stream, mh)
            if mh.encrypted:
                return Extracted(FileStatus.PROTECTED, code="odt_encrypted")
        if "content.xml" not in gz.names:
            return Extracted(FileStatus.CORRUPT, code="odt_no_body")
        sink = TextSink(max_chars)
        with gz.open("content.xml", MAX_PART_BYTES) as stream:
            stopped = run_sax(stream, _Handler(sink, gz))
        return sink.result(stopped)

    return guarded(run)

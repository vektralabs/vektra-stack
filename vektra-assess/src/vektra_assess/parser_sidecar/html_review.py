"""Quiz review HTML to plain text (REQ-101, COMP-008).

The HTML comes from Moodle but carries student-authored content: it is parsed with
html5lib (stdlib etree builder), never rendered and never fetched. Everything that could
execute or load is dropped; images become a placeholder; the student's answer, which
Moodle keeps in form state, is preserved as text. The output is plain text, and escaping
it for display stays the renderer's job.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import html5lib

from vektra_assess.parser_protocol import Extracted, FileStatus
from vektra_assess.parser_sidecar.text import clean, serialize_xml

IMAGE_PLACEHOLDER = "[immagine non visibile all'assistente]"
MEDIA_PLACEHOLDER = "[contenuto multimediale non visibile all'assistente]"

_DROP = frozenset(
    "head script style noscript iframe object embed link meta base template "
    "source track param button datalist".split()
)
_BLOCK = frozenset(
    "address article aside blockquote body dd details div dl dt fieldset "
    "figcaption figure footer form h1 h2 h3 h4 h5 h6 header hr html li main nav "
    "ol p section summary ul legend caption table tbody thead tfoot".split()
)
_TEX_OPEN = re.compile(r"\\\(|\\\[|\$\$")
_TEX_CLOSE = {"\\(": "\\)", "\\[": "\\]", "$$": "$$"}
MAX_TEX_SPAN = 2000  # a longer "span" is prose with a stray opener, not a formula
_WS = re.compile(r"[ \t\r\n\f]+")
_NO_VALUE_INPUTS = frozenset("hidden password submit button image reset file".split())


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _collapse(text: str) -> str:
    """HTML whitespace semantics, except TeX spans which are kept verbatim.

    Linear: the next closer of each kind is searched once and reused, so a node made of
    millions of unmatched openers costs one pass, not one pass per opener.
    """
    out: list[str] = []
    pos = 0
    next_close: dict[str, int] = {}
    for m in _TEX_OPEN.finditer(text):
        if m.start() < pos:
            continue
        closer = _TEX_CLOSE[m.group(0)]
        at = next_close.get(closer, -2)
        if at != -1 and at < m.end():
            at = text.find(closer, m.end())
            next_close[closer] = at
        if at == -1 or at - m.end() > MAX_TEX_SPAN:
            continue
        end = at + len(closer)
        out.append(_WS.sub(" ", text[pos : m.start()]))
        out.append(text[m.start() : end])
        pos = end
    out.append(_WS.sub(" ", text[pos:]))
    return "".join(out)


def _text_of(el: Any) -> str:
    return "".join(el.itertext())


def _tex_annotation(el: Any) -> str | None:
    for d in el.iter():
        if _local(d.tag) == "annotation" and "tex" in (d.get("encoding") or "").lower():
            return _text_of(d)
    return None


Token = tuple[str, str]


def _tokens(root: Any) -> Iterator[Token]:
    stack: list[tuple[Any, bool]] = [(root, False)]
    while stack:
        el, closing = stack.pop()
        name = _local(el.tag)
        if closing:
            if name in ("tr", "td", "th", "table"):
                yield (
                    {"tr": "tr-", "td": "td-", "th": "td-", "table": "tbl-"}[name],
                    "",
                )
            if name in _BLOCK:
                yield ("b", "")
            if el.tail:
                yield ("t", el.tail)
            continue
        leaf: str | None = None
        if name in _DROP:
            leaf = ""
        elif name == "img":
            alt = (el.get("alt") or el.get("title") or "").strip()
            leaf = IMAGE_PLACEHOLDER + (f" {alt}" if alt else "")
        elif name == "svg":
            alt = " ".join(
                _text_of(d).strip()
                for d in el.iter()
                if _local(d.tag) in ("title", "desc")
            ).strip()
            leaf = IMAGE_PLACEHOLDER + (f" {alt}" if alt else "")
        elif name in ("audio", "video"):
            leaf = MEDIA_PLACEHOLDER
        elif name == "math":
            leaf = _tex_annotation(el) or serialize_xml(el)
        elif name == "input":
            kind = (el.get("type") or "text").lower()
            if kind in ("radio", "checkbox"):
                leaf = "[x] " if el.get("checked") is not None else "[ ] "
            elif kind not in _NO_VALUE_INPUTS:
                leaf = f"[risposta: {el.get('value') or ''}]"
            else:
                leaf = ""
        elif name == "select":
            chosen = [
                _WS.sub(" ", _text_of(o)).strip()
                for o in el.iter()
                if _local(o.tag) == "option" and o.get("selected") is not None
            ]
            leaf = f"[selezione: {', '.join(chosen)}]"
        elif name == "textarea":
            yield ("b", "")
            yield ("block", _text_of(el))
            yield ("b", "")
            if el.tail:
                yield ("t", el.tail)
            continue
        elif name == "pre":
            yield ("pre", _text_of(el))
            if el.tail:
                yield ("t", el.tail)
            continue
        elif name == "code":
            yield ("raw", "`" + _text_of(el) + "`")
            if el.tail:
                yield ("t", el.tail)
            continue
        if leaf is not None:
            if leaf:
                yield ("raw", leaf)
            if el.tail:
                yield ("t", el.tail)
            continue
        if name == "br":
            yield ("br", "")
        if name in _BLOCK:
            yield ("b", "")
        if name == "li":
            yield ("raw", "- ")
        if name == "table":
            yield ("tbl+", "")
        elif name == "tr":
            yield ("tr+", "")
        elif name in ("td", "th"):
            yield ("td+", "")
        if el.text:
            yield ("t", el.text)
        stack.append((el, True))
        stack.extend((c, False) for c in reversed(list(el)))


def _assemble(tokens: Iterator[Token]) -> str:
    lines: list[tuple[str, bool]] = []  # (text, verbatim)
    cur: list[str] = []
    depth = 0
    cells: list[str] | None = None
    cell: list[str] | None = None

    def flush() -> None:
        text = "".join(cur).strip()
        cur.clear()
        if text:
            lines.append((text, False))

    for kind, val in tokens:
        if cell is not None and kind in ("b", "br"):
            cell.append(" ")
            continue
        if kind == "tbl+":
            if depth == 0:
                flush()
            depth += 1
        elif kind == "tbl-":
            depth = max(0, depth - 1)
        elif kind == "tr+" and depth == 1:
            cells = []
        elif kind == "td+" and depth == 1 and cells is not None:
            cell = []
        elif kind == "td-" and depth == 1 and cells is not None and cell is not None:
            cells.append(_WS.sub(" ", "".join(cell)).strip() if cell else "")
            cell = None
        elif kind == "tr-" and depth == 1 and cells is not None:
            lines.append(("| " + " | ".join(cells) + " |", False))
            cells = None
        elif kind in ("t", "raw"):
            piece = _collapse(val) if kind == "t" else val
            (cell if cell is not None else cur).append(piece)
        elif kind == "pre":
            if cell is not None:
                cell.append(val)
            else:
                flush()
                lines.append(("```", True))
                lines.extend((ln, True) for ln in val.split("\n"))
                lines.append(("```", True))
        elif kind == "block":
            flush()
            lines.extend((ln, True) for ln in val.split("\n"))
        elif kind in ("b", "br"):
            flush()
    flush()
    out: list[str] = []
    blank = 0
    for text, _verbatim in lines:
        if not text.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(text)
    return "\n".join(out).strip("\n")


def sanitize_review_html(data: bytes, max_chars: int) -> Extracted:
    try:
        source = data.decode("utf-8", errors="replace")
        doc = html5lib.parse(source, treebuilder="etree", namespaceHTMLElements=False)
        text = clean(_assemble(_tokens(doc)))
    except RecursionError:
        return Extracted(FileStatus.CORRUPT, code="html_too_deep")
    if not text.strip():
        return Extracted(FileStatus.NO_TEXT, code="empty")
    if len(text) > max_chars:
        return Extracted(FileStatus.READ_PARTIAL, text[:max_chars], code="char_cap")
    return Extracted(FileStatus.READ, text)

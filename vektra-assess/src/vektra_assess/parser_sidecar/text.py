"""Plain text, Markdown, source code and markup: read as text, never executed or rendered."""

from __future__ import annotations

import unicodedata
from typing import Any

from vektra_assess.parser_protocol import Extracted, FileStatus

_STRIP = {c for c in range(0x20) if c not in (9, 10)} | {0x7F}
_TABLE = {c: None for c in _STRIP}


def clean(text: str) -> str:
    """NFC, C0 controls (except newline and tab) dropped, CRLF normalised."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", text.translate(_TABLE))


def decode(data: bytes) -> str | None:
    """UTF-8 (BOM tolerated), else cp1252, else latin-1; None only for broken UTF-16."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        pass
    # cp1252 leaves 0x81, 0x8D, 0x8F, 0x90 and 0x9D undefined; legacy text that holds one
    # is still text. latin-1 maps every byte, so this never fails.
    return data.decode("latin-1")


def extract_text(data: bytes, max_chars: int) -> Extracted:
    raw = decode(data)
    if raw is None:
        return Extracted(FileStatus.NO_TEXT, code="undecodable")
    text = clean(raw)
    if not text.strip():
        return Extracted(FileStatus.NO_TEXT, code="empty")
    if len(text) > max_chars:
        return Extracted(FileStatus.READ_PARTIAL, text[:max_chars], code="char_cap")
    return Extracted(FileStatus.READ, text)


def _localname(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def serialize_xml(el: Any) -> str:
    """MathML (or any subtree) as source text, iteratively, without namespace noise."""
    out: list[str] = []
    stack: list[tuple[Any, bool]] = [(el, False)]
    while stack:
        node, closing = stack.pop()
        name = _localname(node.tag)
        if closing:
            out.append(f"</{name}>")
            if node.tail and node is not el:
                out.append(_esc(node.tail))
            continue
        attrs = "".join(
            f' {_localname(k)}="{_esc(str(v)).replace(chr(34), "&quot;")}"'
            for k, v in node.attrib.items()
        )
        out.append(f"<{name}{attrs}>")
        if node.text:
            out.append(_esc(node.text))
        stack.append((node, True))
        stack.extend((c, False) for c in reversed(list(node)))
    return "".join(out)

"""Post-render allowlist check of a transcript (BR-016, inert by construction). Fail closed.

The template escapes everything and builds MathML from an allowlisted tree; this check is the
second, independent line: it re-parses the rendered bytes with the stdlib ``html.parser`` and
refuses the document unless

- every start tag is in ``TAGS`` and every attribute is in that tag's own set;
- no attribute value contains a URL scheme or a ``//`` reference (``data:``, ``javascript:``,
  ``http:`` ...), and none is longer than ``MAX_ATTR``;
- there is exactly one ``<style>``, inside ``<head>``, and its content hashes to ``STYLE_SHA256``
  (the template's own stylesheet: no ``@import``, no ``url()``);
- there is no comment, processing instruction or declaration other than ``<!DOCTYPE html>``;
- the document is valid UTF-8.

``check(rendered)`` raises ``AllowlistViolation`` with a short content-free reason. The generator
calls it on every transcript before encrypting it.
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser

from vektra_assess.transcript.mathml import MATH_ATTRS, MATH_TAGS

_HTML_TAGS: dict[str, frozenset[str]] = {
    "html": frozenset({"lang"}),
    "head": frozenset(),
    "meta": frozenset({"charset"}),
    "title": frozenset(),
    "style": frozenset(),
    "body": frozenset(),
    "header": frozenset(),
    "main": frozenset(),
    "footer": frozenset(),
    "section": frozenset({"aria-labelledby"}),
    "h1": frozenset({"id"}),
    "h2": frozenset({"id"}),
    "h3": frozenset({"id"}),
    "p": frozenset({"class", "lang"}),
    "dl": frozenset(),
    "dt": frozenset(),
    "dd": frozenset(),
    "ul": frozenset(),
    "ol": frozenset(),
    "li": frozenset({"class", "lang"}),
    "span": frozenset({"class"}),
    "strong": frozenset(),
    "code": frozenset({"aria-label"}),
    "time": frozenset({"datetime"}),
}
TAGS: dict[str, frozenset[str]] = {
    **_HTML_TAGS,
    **{t: MATH_ATTRS for t in MATH_TAGS},
}
#: void elements of the template (no end tag)
VOID = frozenset({"meta"})
MAX_ATTR = 200
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*\s*:|//|\\\\")
#: ``datetime`` is the only attribute whose value has a colon: checked by shape instead
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class AllowlistViolation(ValueError):
    """The rendered transcript is not inside the allowlist; ``reason`` is content-free."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


#: SHA-256 of the template's stylesheet (``templates/transcript.css``). A test recomputes it.
STYLE_SHA256 = "ebcf382f8de374bbe878ebc5b47f17c27d5db294a62c6d4ca5f4ba1835f170bd"


class _Checker(HTMLParser):
    def __init__(self, style_sha256: str) -> None:
        super().__init__(convert_charrefs=True)
        self.style_sha256 = style_sha256
        self.stack: list[str] = []
        self.styles = 0
        self.in_style = False
        self.style_text: list[str] = []
        self.doctype = 0

    def _fail(self, reason: str) -> None:
        raise AllowlistViolation(reason)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        allowed = TAGS.get(tag)
        if allowed is None:
            self._fail(f"tag:{tag[:20]}")
            return
        seen: set[str] = set()
        for name, value in attrs:
            if name not in allowed or name in seen:
                self._fail(f"attr:{tag[:20]}:{name[:20]}")
            seen.add(name)
            v = value or ""
            if name == "datetime":
                if not _DATETIME.match(v):
                    self._fail(f"attr_value:{tag[:20]}:{name[:20]}")
            elif len(v) > MAX_ATTR or _SCHEME.search(v):
                self._fail(f"attr_value:{tag[:20]}:{name[:20]}")
        if tag == "style":
            if self.stack != ["html", "head"] or self.styles:
                self._fail("style_position")
            self.styles += 1
            self.in_style = True
        if tag == "math" and "math" in self.stack:
            self._fail("nested_math")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self._fail(f"end_tag:{tag[:20]}")
        self.stack.pop()
        if tag == "style":
            self.in_style = False
            digest = hashlib.sha256("".join(self.style_text).encode("utf-8"))
            if digest.hexdigest() != self.style_sha256:
                self._fail("style_hash")

    def handle_data(self, data: str) -> None:
        if self.in_style:
            self.style_text.append(data)

    def handle_comment(self, data: str) -> None:
        self._fail("comment")

    def handle_decl(self, decl: str) -> None:
        if decl.strip().lower() != "doctype html" or self.doctype or self.stack:
            self._fail("declaration")
        self.doctype += 1

    def handle_pi(self, data: str) -> None:
        self._fail("processing_instruction")

    def unknown_decl(self, data: str) -> None:
        self._fail("declaration")


def check(rendered: bytes, *, style_sha256: str | None = None) -> None:
    try:
        doc = rendered.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise AllowlistViolation("not_utf8") from None
    checker = _Checker(style_sha256 or STYLE_SHA256)
    checker.feed(doc)
    checker.close()
    if checker.stack:
        raise AllowlistViolation("unclosed")
    if checker.styles != 1 or checker.doctype != 1:
        raise AllowlistViolation("structure")

"""TeX in turn text to static, presentation-only MathML (BR-016), or escaped TeX.

``segments(text)`` splits a turn into text and math segments. Recognised delimiters: ``\\( \\)``,
``\\[ \\]`` and ``$$ $$`` (a single ``$`` is never math: it is a currency sign far more often).
Each TeX segment goes through ``latex2mathml`` (pure Python), and the result is RE-PARSED and
rebuilt as a ``MathNode`` tree that holds only allowlisted presentation elements and attributes
(no ``href``, ``xlink:*``, ``maction``, ``semantics``, ``annotation``/``annotation-xml``, no
``xmlns``). The template renders that tree element by element with autoescaping, so no markup
string ever reaches the output. Any conversion or filter failure (an unknown element, an
attribute outside the list, a parse error, an oversized formula) yields ``Tex``: the source shown
as escaped text in ``<code>`` with a constant ``aria-label``. User text never lands in an
attribute value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

MATH_TAGS = frozenset(
    "math mrow mi mn mo ms mtext mspace mfrac msqrt mroot msub msup msubsup munder mover "
    "munderover mtable mtr mtd mstyle".split()
)
MATH_ATTRS = frozenset(
    "display mathvariant stretchy fence separator accent columnalign".split()
)
#: attribute values the filter accepts (a value is never user text, but it is still checked)
_VALUE = re.compile(r"^[A-Za-z0-9 .\-]{0,32}$")
MAX_TEX_CHARS = 2000
#: deepest MathML nesting rendered as MathML (CodeRabbit 4503029399); deeper -> escaped TeX
MAX_DEPTH = 32

_DELIMS = re.compile(r"\\\((.+?)\\\)|\\\[(.+?)\\\]|\$\$(.+?)\$\$", re.DOTALL)


@dataclass(frozen=True)
class MathNode:
    tag: str
    attrs: tuple[tuple[str, str], ...] = ()
    children: tuple[MathNode | str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Text:
    text: str
    kind: str = "text"


@dataclass(frozen=True)
class Math:
    node: MathNode
    kind: str = "math"


@dataclass(frozen=True)
class Tex:
    tex: str
    kind: str = "tex"


Segment = Text | Math | Tex


class _Reject(ValueError):
    pass


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, list[tuple[str, str]], list[MathNode | str]]] = [
            ("#root", [], [])
        ]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in MATH_TAGS:
            raise _Reject(tag)
        kept: list[tuple[str, str]] = []
        for name, value in attrs:
            if name == "xmlns" and tag == "math":
                continue  # the HTML parser needs no namespace; it is a URL, never emitted
            if name not in MATH_ATTRS or value is None or not _VALUE.match(value):
                raise _Reject(name)
            kept.append((name, value))
        if len(self.stack) > MAX_DEPTH:
            # the template renders the tree recursively: a deep tree would exhaust Jinja's
            # recursion and fail the whole transcript; the TeX fallback shows it instead
            raise _Reject("depth")
        self.stack.append((tag, kept, []))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        name, attrs, children = self.stack.pop()
        if name != tag:
            raise _Reject("nesting")
        self.stack[-1][2].append(MathNode(name, tuple(attrs), tuple(children)))

    def handle_data(self, data: str) -> None:
        if len(self.stack) == 1:
            if data.strip():
                raise _Reject("text outside math")
            return
        self.stack[-1][2].append(data)

    def handle_comment(self, data: str) -> None:
        raise _Reject("comment")

    def handle_decl(self, decl: str) -> None:
        raise _Reject("declaration")

    def handle_pi(self, data: str) -> None:
        raise _Reject("processing instruction")

    def unknown_decl(self, data: str) -> None:
        raise _Reject("declaration")


def filter_mathml(markup: str) -> MathNode:
    """Re-parse converter output into an allowlisted tree; raises ``ValueError`` otherwise."""
    builder = _Builder()
    builder.feed(markup)
    builder.close()
    root = builder.stack
    if len(root) != 1 or len(root[0][2]) != 1:
        raise _Reject("not exactly one math element")
    node = root[0][2][0]
    if not isinstance(node, MathNode) or node.tag != "math":
        raise _Reject("root is not math")
    return node


def tex_to_math(tex: str, *, display: bool) -> Math | Tex:
    if not tex.strip() or len(tex) > MAX_TEX_CHARS:
        return Tex(tex)
    try:
        from latex2mathml.converter import convert

        markup = convert(tex, display="block" if display else "inline")
        return Math(filter_mathml(markup))
    except Exception:  # any converter or filter failure: escaped TeX, never a guess
        return Tex(tex)


def segments(text: str) -> list[Segment]:
    out: list[Segment] = []
    pos = 0
    for m in _DELIMS.finditer(text):
        if m.start() > pos:
            out.append(Text(text[pos : m.start()]))
        inline, block, dollars = m.group(1), m.group(2), m.group(3)
        tex = (
            inline if inline is not None else (block if block is not None else dollars)
        )
        out.append(tex_to_math(tex, display=inline is None))
        pos = m.end()
    if pos < len(text):
        out.append(Text(text[pos:]))
    return out

"""Sentence segmentation for check-then-release streaming (plan assess-llm-runner, task B).

A sentence ends at ``.``, ``?``, ``!`` (a run of them counts as one end) or at a newline, but only
outside a citation tag ``[...]``, a machine marker ``[[...]]`` and a quotation, and only when the
point is not part of a number (``3.5``, ``3.5/10``), a page reference (``p.12``, ``pag. 3``) or a
common abbreviation. A point at the very end of the buffer is ambiguous while the stream is open
(``3.`` may become ``3.5``), so it is held until the next chunk or the end of the stream.

A quotation opened right after a teacher-quote lead ("Commento del docente: \"...") is never
demoted to text: it is held until it closes, however long, and if the stream ends with it still
open the whole rest is one sentence. Demoting it would release its fragments as plain sentences
that the verbatim-feedback check (``feedback_quote``) cannot recognise as the teacher's words; held
whole, the check sees the lead and the quote together and replaces a quote it cannot confirm.

``split`` is incremental: it returns the complete sentences and the unfinished rest, which the
caller keeps in its buffer. With ``final=True`` the rest is returned as the last sentence.
"""

from __future__ import annotations

import re

from vektra_assess.llm.postcheck.feedback_quote import LEAD

_TERMINATORS = ".?!"
_OPEN_CLOSE = {"“": "”", "«": "»"}
_CLOSERS = set(_OPEN_CLOSE.values())
# words that end with a point without ending the sentence (compared lower-case, without the point).
# A word that can also close a sentence ("no", "max") is left out: merging two sentences only
# delays a release, splitting one in the middle of a reference would check half a claim
ABBREVIATIONS = frozenset(
    {
        "p", "pp", "pag", "pagg", "es", "ecc", "cfr", "art", "n", "nr", "fig", "tab", "cap",
        "par", "sez", "vol", "sig", "prof", "dott", "ing", "eq", "vs", "e.g", "i.e", "etc",
        "pt", "pts", "approx",
    }
)  # fmt: skip
_LEAD_END = re.compile(rf"{LEAD}\s*:?\s*$", re.IGNORECASE)
_WORD_BEFORE = re.compile(r"([A-Za-zÀ-ÿ](?:[A-Za-zÀ-ÿ.]*[A-Za-zÀ-ÿ])?)$")


def _is_abbreviation(text: str, dot: int) -> bool:
    m = _WORD_BEFORE.search(text, 0, dot)
    return m is not None and m.group(1).lower() in ABBREVIATIONS


#: an opener ([ or a quote) left open longer than this is taken as a literal character: a half-open
#: interval "[0, 1)" or a stray quote must not freeze every sentence end after it
MAX_HOLD = 300


def split(buffer: str, *, final: bool = False) -> tuple[list[str], str]:
    """``(complete sentences, rest)``; sentences keep their own text, stripped of outer spaces."""
    literal: set[int] = set()  # opener positions demoted to plain characters
    while True:
        done, rest, stuck = _scan(buffer, final, literal)
        if stuck is None:
            return done, rest
        literal.add(stuck)  # rescan with that opener as text


def _scan(
    buffer: str, final: bool, literal: set[int]
) -> tuple[list[str], str, int | None]:
    """One pass. Returns the position of an opener to demote, or None when the pass is final."""
    out: list[str] = []
    start = 0
    square = 0  # depth of [...] (a marker [[...]] counts twice)
    square_at: list[int] = []
    quote: str | None = None  # the closer we are waiting for
    quote_at = -1
    quote_led = False  # the open quotation follows a teacher-quote lead: never demoted
    i, n = 0, len(buffer)
    while i < n:
        c = buffer[i]
        if quote is not None:
            if i - quote_at > MAX_HOLD and not quote_led:
                return [], "", quote_at
            if c == quote:
                quote = None
                if i and buffer[i - 1] in _TERMINATORS and not square:
                    # «... fine.» Poi: the quoted sentence also ends ours when what follows
                    # starts a new one (a capital); «... fine.» e basta: it does not
                    k = i + 1
                    while k < n and buffer[k] in " \t":
                        k += 1
                    if k >= n and not final:
                        break  # the next chunk decides
                    if k >= n or buffer[k] == "\n" or buffer[k].isupper():
                        _emit(out, buffer[start : i + 1])
                        start = i + 1
            i += 1
            continue
        if square and (c == "\n" or i - square_at[0] > MAX_HOLD):
            # a tag or a marker never spans a line or a paragraph: the opener was text
            return [], "", square_at[0]
        if i in literal:
            pass
        elif c == '"' or c in _OPEN_CLOSE:
            quote, quote_at = _OPEN_CLOSE.get(c, '"'), i
            quote_led = _LEAD_END.search(buffer, start, i) is not None
        elif c == "[":
            square += 1
            square_at.append(i)
        elif c == "]" and square:
            square -= 1
            square_at.pop()
        elif square:
            pass
        elif c == "\n":
            _emit(out, buffer[start:i])
            start = i + 1
        elif c in _TERMINATORS:
            j = i
            while j + 1 < n and buffer[j + 1] in _TERMINATORS:
                j += 1
            if j + 1 >= n and not final:
                break  # the next chunk decides: "3." may be "3.5", "?" may be "?!"
            nxt = buffer[j + 1] if j + 1 < n else ""
            if c == "." and j == i:
                if nxt.isdigit() or (nxt and not nxt.isspace() and nxt not in '"”»)]'):
                    i += 1  # 3.5, p.12, www.x.it, a.b: not an end
                    continue
                if _is_abbreviation(buffer, i):
                    i += 1
                    continue
            # a closing quote or bracket right after the point belongs to this sentence
            k = j + 1
            while k < n and buffer[k] in '"”»)':
                k += 1
            _emit(out, buffer[start:k])
            start = k
            i = k
            continue
        i += 1
    if final and quote is not None and quote_led:
        # a teacher quotation still open at the end: one sentence, so the quote check sees it whole
        _emit(out, buffer[start:])
        return out, "", None
    if final and (quote is not None or square):
        # the stream ended with an opener still open: it was text, split without it
        return [], "", quote_at if quote is not None else square_at[0]
    rest = buffer[start:]
    if final:
        _emit(out, rest)
        rest = ""
    return out, rest, None


def _emit(out: list[str], piece: str) -> None:
    s = piece.strip()
    if s:
        out.append(s)

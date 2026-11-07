"""Reply language of a turn (REQ-100): Italian, English or other, from the latest student message.

Dependency-free: a stopword vote. Italian and English are the supported languages; a message
whose stopwords are mostly from another language is ``other`` (the runner then replies in Italian
and prepends the shipped explanatory sentence). A message written mostly in a non-Latin script
(Arabic, Chinese, Cyrillic, Japanese ...) has no stopword to vote with, so its letters decide: it is
``other`` too. A tie, or a message with no stopwords and no such letters ("ok", "Domanda 3?",
"3.5/10"), is Italian, the default.
"""

from __future__ import annotations

import re
from typing import Literal

Language = Literal["it", "en", "other"]

_IT = frozenset(
    """il lo la i gli le un uno una di del dello della dei degli delle a al allo alla ai agli alle
    da dal dalla in nel nella nei nelle con su sul sulla per tra fra e ed o ma se che chi cosa come
    perché perche quando dove non è sono era ho hai ha abbiamo avete hanno mi ti si ci vi mio mia
    tuo tua suo sua questo questa quello quella anche più molto poi già ancora sì no grazie ciao
    voto domanda risposta perché qual quale quali posso puoi può vorrei capito capire spiegami
    sbagliato giusto docente esame""".split()
)
_EN = frozenset(
    """the a an of to in on at for with by from and or but if that this these those is are was
    were be been have has had do does did i you he she it we they my your his her its our their
    what which who why how when where not no yes can could would should will please thanks hi
    question answer grade mark teacher wrong right understand explain why""".split()
)
_OTHER = {
    "fr": frozenset(
        "le la les des du un une et est sont je tu il elle nous vous ils pas pourquoi comment "
        "ma mon ton votre réponse question note professeur merci bonjour avec pour dans sur ce "
        "cette qui que ne".split()
    ),
    "es": frozenset(
        "el la los las un una unos y es son yo tú él ella nosotros por qué cómo mi tu su "
        "respuesta pregunta nota profesor gracias hola con para en del al lo que no pero "
        "está estoy".split()
    ),
    "de": frozenset(
        "der die das den dem des ein eine und ist sind ich du er sie wir ihr nicht warum wie "
        "mein dein antwort frage note lehrer danke hallo mit für auf zu von was bitte".split()
    ),
}
_WORD = re.compile(r"[a-zà-ÿ']+", re.IGNORECASE)
# letters of a script other than Latin (Latin, Latin-1, Latin Extended A/B and Additional excluded)
_LATIN_RANGES = ((0x0000, 0x024F), (0x1E00, 0x1EFF))
#: fewer non-Latin letters than this is not substantive input (a stray symbol, a lone kanji)
_MIN_SCRIPT_LETTERS = 2


def _non_latin_letters(text: str) -> tuple[int, int]:
    """``(non-Latin letters, Latin letters)`` of the text."""
    other = latin = 0
    for ch in text:
        if not ch.isalpha():
            continue
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in _LATIN_RANGES):
            latin += 1
        else:
            other += 1
    return other, latin


def detect(text: str) -> Language:
    script, latin = _non_latin_letters(text)
    if script >= _MIN_SCRIPT_LETTERS and script > latin:
        return "other"  # unsupported script: no stopword list could ever vote for it
    words = [w.strip("'") for w in _WORD.findall(text.lower())]
    words = [w for w in words if w]
    it = sum(w in _IT for w in words)
    en = sum(w in _EN for w in words)
    other = max(sum(w in sw for w in words) for sw in _OTHER.values())
    # a word shared by several lists ("la", "no", "in") counts for each; the other languages must
    # clearly outvote both supported ones before a reply falls back to Italian with a notice
    if other >= 2 and other > it and other > en:
        return "other"
    return "en" if en > it else "it"

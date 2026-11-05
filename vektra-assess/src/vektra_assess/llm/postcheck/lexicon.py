"""Verdict, merit, estimate, grade-change, teacher-intent and judgement lexicon (post-check stage 6).

REQ-097 asks to "extend the REQ-044 lexicon", which does not exist in code (vektra_core safeguards
hold only Presidio and passthrough), so this assess-owned lexicon is built here (plan Notes). A
sentence that matches is replaced by the runner with the BR-009 notice, the turn is marked and
``grade_claim_replaced`` is audited with the category only.

Patterns are matched case-insensitively on the sentence with the machine markers already removed;
an apostrophe in a pattern matches both ``'`` and the typographic right single quote (U+2019). A grade-change verb under
an explicit first-person negation directly before it ("non posso cambiare il voto", "I cannot
change your grade") states a limit, not a claim: that match is ignored, and the sentence is
replaced only if another match remains. Anything less clear is still replaced (fail closed).
``LEXICON_VERSION`` is a hash of every pattern (negations included): it enters ``prompt_version``, so editing the lexicon
requires a new NFR-023 activation.
"""

from __future__ import annotations

import hashlib
import re

# category -> patterns (IT and EN). Categories are the audit codes.
PATTERNS: dict[str, tuple[str, ...]] = {
    # "hai ragione / torto", fairness or correctness of the grade (REQ-097, BR-009)
    "verdict": (
        r"\bhai (?:pienamente |perfettamente )?(?:ragione|torto)\b",
        r"\b(?:non )?ha(?:i)? sbagliato il docente\b",
        r"\bil docente ha sbagliato\b",
        r"\b(?:il tuo |il |questo )?(?:voto|punteggio|livello|giudizio)\b[^.?!]{0,30}\b(?:è|sembra|era|appare) "
        r"(?:(?:molto|troppo|un po['\u2019]) )?(?:giust[oa]|corrett[oa]|sbagliat[oa]|equ[oa]|iniqu[oa]|ingiust[oa]|meritat[oa]|immeritat[oa]|bass[oa]|alt[oa]|sever[oa]|generos[oa])\b",
        r"\b(?:valutazione|correzione)\b[^.?!]{0,20}\b(?:ingiusta|sbagliata|scorretta|corretta|giusta|equa|severa)\b",
        r"\bmeritavi\b|\bmeriteresti\b|\bavresti meritato\b",
        r"\byou(?:['\u2019]re| are) (?:absolutely |completely )?(?:right|wrong)\b",
        r"\b(?:grade|mark|score|level)\b[^.?!]{0,30}\b(?:is|was|seems) (?:(?:too|a bit|very) )?(?:fair|unfair|wrong|correct|right|harsh|generous|low|high|deserved|undeserved)\b",
        r"\byou deserved?\b|\bthe teacher (?:was|is) wrong\b",
    ),
    # any claim that the grade is or will be changed (BR-009)
    "grade_change": (
        r"\b(?:alzare|abbassare|aumentare|modificare|cambiare|correggere|rivedere|ritoccare) (?:il (?:tuo )?|un )?(?:voto|punteggio)\b",
        r"\b(?:voto|punteggio)\b[^.?!]{0,20}\b(?:verrà|sarà|può essere|potrà essere) (?:alzat[oa]|modificat[oa]|cambiat[oa]|rivist[oa]|corrett[oa]|aumentat[oa])\b",
        r"\bti (?:darà|daranno|riconoscerà|riconosceranno|aggiungerà) (?:\w+ )?punt[io]\b",
        r"\b(?:raise|increase|lower|change|revise|adjust|bump) (?:your|the) (?:grade|mark|score)\b",
        r"\b(?:grade|mark|score)\b[^.?!]{0,20}\bwill be (?:changed|raised|revised|adjusted|increased)\b",
    ),
    # strength or merit ratings of an argument (REQ-097 forbids both ways)
    "merit": (
        r"\b(?:argomento|argomentazione|obiezione|contestazione|punto)\b[^.?!]{0,15}\b(?:forte|debole|solid[oa]|convincente|fondat[oa]|infondat[oa]|valid[oa]|debole)\b",
        r"\b(?:ottim[oa]|buon[oa]?|debole|fort[ei]) (?:argomento|obiezione|contestazione)\b",
        r"\b(?:strong|weak|compelling|convincing|valid|solid|good) (?:argument|point|objection|case)\b",
    ),
    # predictions or estimates of the grade or of the oral (R-GRADE)
    "estimate": (
        r"\b(?:probabilmente|sicuramente|forse) (?:otterrai|prenderai|passerai|supererai|recupererai)\b",
        r"\ball['\u2019]orale (?:otterrai|prenderai|andrà|recupererai|passerai)\b",
        r"\b(?:il (?:tuo )?voto|il punteggio)\b[^.?!]{0,20}\b(?:potrebbe|dovrebbe|potrà|salirà|scenderà)\b",
        r"\b(?:prevedo|stimo|la mia stima)\b",
        r"\byou(?:['\u2019]ll| will) (?:probably |likely |surely )?(?:get|pass|fail|score|recover)\b",
        r"\b(?:your|the) (?:grade|mark|score)\b[^.?!]{0,20}\b(?:could|should|might|would|will) (?:go|be|change|rise|drop|increase)\b",
        r"\bmy (?:estimate|prediction)\b|\bi (?:predict|estimate|expect you)\b",
    ),
    # intent attributed to the teacher beyond the written feedback (REQ-096 HARD)
    "teacher_intent": (
        r"\b(?:il|la) (?:docente|professore|professoressa|prof)\b[^.?!]{0,10}\b(?:voleva|intendeva|pensava|pensa|crede|credeva|ritiene|riteneva|si aspettava|cercava|avrebbe voluto|vuole)\b",
        r"\b(?:l['\u2019]intenzione|lo scopo|il pensiero) del (?:docente|professore)\b",
        r"\bthe (?:teacher|instructor|professor)\b[^.?!]{0,10}\b(?:meant|wanted|intended|thinks|thought|believes|believed|expected|was looking for)\b",
    ),
    # judgements of the person, not of the answer (REQ-100)
    "person": (
        r"\bsei (?:stat[oa] )?(?:molto |davvero |proprio )?(?:brav[oa]|pigr[oa]|incapace|intelligente|scars[oa]|negat[oa]|superficiale|distratt[oa]|geniale|un genio|portat[oa])\b",
        r"\bnon sei portat[oa]\b",
        r"\byou(?:['\u2019]re| are) (?:very |really |so )?(?:lazy|smart|careless|stupid|brilliant|clever|bad at|good at|a genius|talented)\b",
    ),
    # sarcasm, belittling or inflated wording (REQ-100)
    "tone": (
        r"\bbanal[ei]\b|\bbanalmente\b|\bfacil[ei]\b|\bfacilissim[oa]\b|\belementare\b|\bovviamente\b",
        r"\bcome chiunque\b|\bpersino tu\b|\bperfino tu\b",
        r"\b(?:trivial|obviously|it was easy|it is easy|anyone could|even you)\b",
    ),
}

# a first-person negation directly governing a verb-led grade-change pattern (must end right
# before the verb). Only the patterns listed in _NEGATABLE may be cleared by it
NEGATIONS: tuple[str, ...] = (
    r"\b(?:io |noi )?non (?:posso|possiamo|potrei|potremmo|sono in grado di|siamo in grado di|"
    r"ho (?:il potere|l['\u2019]autorità|modo) di|(?:mi |ci )?è possibile)\s+$",
    r"\b(?:i|we) (?:cannot|can not|can['\u2019]t|could not|couldn['\u2019]t|am not able to|"
    r"are not able to|am unable to|are unable to|have no (?:power|authority) to|do not|don['\u2019]t|"
    r"will not|won['\u2019]t)\s+$",
)
_NEGATABLE = {
    ("grade_change", 0),
    ("grade_change", 3),
}  # the verb-led IT and EN patterns
_NEGATION = tuple(re.compile(p, re.IGNORECASE) for p in NEGATIONS)
# another negation before it may reverse it ("non è vero che non posso ..."): then it clears nothing
_OUTER_NEGATION = re.compile(
    r"\b(?:non|not|never|mai|nessun\w*)\b|n['\u2019]t\b", re.IGNORECASE
)

_COMPILED = {
    cat: tuple(re.compile(p, re.IGNORECASE) for p in pats)
    for cat, pats in PATTERNS.items()
}

LEXICON_VERSION = hashlib.sha256(
    "\n".join(
        [f"{c}\t{p}" for c, ps in sorted(PATTERNS.items()) for p in ps]
        + [f"negation\t{p}" for p in NEGATIONS]
    ).encode()
).hexdigest()[:8]


def scan(sentence: str) -> str | None:
    """The first category the sentence matches, or None (a grade change before an estimate)."""
    for cat, rxs in _COMPILED.items():
        for n, rx in enumerate(rxs):
            for m in rx.finditer(sentence):
                if (cat, n) in _NEGATABLE and _negated(sentence, m.start()):
                    continue
                return cat
    return None


def _negated(sentence: str, at: int) -> bool:
    """A clear first-person negation ends right before ``at`` and nothing before it negates it."""
    before = sentence[:at]
    for rx in _NEGATION:
        m = rx.search(before)
        if m is not None:
            return _OUTER_NEGATION.search(before, 0, m.start()) is None
    return False

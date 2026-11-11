"""Question kinds the bot discusses item by item (REQ-101), and which question states are final.

Both are Vektra's vocabulary (``vektra_shared.lms.QuestionKind`` / ``QuestionState``, ADR-0033
D2): the LMS provider maps its own question types and states onto them, and maps anything it
cannot place onto ``QuestionKind.OTHER`` (kept at mark-and-feedback level and flagged) or
``QuestionState.UNKNOWN`` (never final: fail closed).
"""

from __future__ import annotations

from vektra_shared.lms import QuestionKind, QuestionState

DISCUSSABLE_KINDS: frozenset[QuestionKind] = frozenset(
    {
        QuestionKind.MULTIPLE_CHOICE,
        QuestionKind.TRUE_FALSE,
        QuestionKind.SHORT_ANSWER,
        QuestionKind.NUMERICAL,
        QuestionKind.ESSAY,
        QuestionKind.MATCHING,
        QuestionKind.EMBEDDED_ANSWERS,
        QuestionKind.GAP_SELECT,
    }
)

#: information items: never graded, ignored by "fully graded" (BR-014)
INFORMATION_KINDS: frozenset[QuestionKind] = frozenset({QuestionKind.INFORMATION})

#: question states with a FINAL mark. ``PENDING`` is not final; neither is ``UNKNOWN``
#: (fail closed): a provider state it could not place never counts as graded.
FINAL_STATES: frozenset[QuestionState] = frozenset(
    {QuestionState.FINAL_MARKED, QuestionState.FINAL_NO_MARK}
)

#: final, but the LMS shows no mark (the question was not answered)
NO_MARK_FINAL_STATES: frozenset[QuestionState] = frozenset(
    {QuestionState.FINAL_NO_MARK}
)

#: kinds whose question text and response are extracted from the review HTML by allowlist
#: (ADR-0032 amendment 2026-11-04), with the provider's extraction profile (ADR-0033 D4). A kind
#: is enabled only where the provider's per-type oracle on its recorded reviews proves the right
#: answer, the feedback and the canaries absent from the output. Not enabled, and why:
#: - embedded answers (cloze): no question-text / response block, the gaps live in the text;
#: - matching, gap select: extraction looks clean, but every recorded attempt is unanswered, so
#:   no per-item feedback or correctness markup has been seen yet. Enable after a graded
#:   recording.
EXTRACTION_KINDS: frozenset[QuestionKind] = frozenset(
    {
        QuestionKind.MULTIPLE_CHOICE,
        QuestionKind.TRUE_FALSE,
        QuestionKind.SHORT_ANSWER,
        QuestionKind.NUMERICAL,
        QuestionKind.ESSAY,
    }
)


def is_discussable(kind: QuestionKind) -> bool:
    return kind in DISCUSSABLE_KINDS


def is_final(state: QuestionState) -> bool:
    """Whether a reviewed item's state carries a final mark; ``UNKNOWN`` never does."""
    return state in FINAL_STATES

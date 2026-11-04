"""Question types the bot discusses item by item (REQ-101), and Moodle's question states.

The discussable list was verified on Moodle 4.3 and 5.2 by the OQ-025 spike and the plan 4
recordings (``tests/fixtures/moodle_ws/*/mod_quiz_get_attempt_review__types-*``). Any other type
(``ddwtos``, ``ddmarker``, ``calculated`` ...) is kept at mark-and-feedback level and flagged.
"""

from __future__ import annotations

DISCUSSABLE_QTYPES: frozenset[str] = frozenset(
    {
        "multichoice",
        "truefalse",
        "shortanswer",
        "numerical",
        "essay",
        "match",
        "multianswer",
        "gapselect",
    }
)

#: information items: never graded, ignored by "fully graded" (BR-014)
INFORMATION_QTYPES: frozenset[str] = frozenset({"description"})

#: question states with a FINAL mark (``question_state`` in Moodle core). "todo", "invalid",
#: "complete" and "needsgrading" are not final; neither is an unknown state (fail closed).
FINAL_STATES: frozenset[str] = frozenset(
    {
        "gaveup",
        "gradedwrong",
        "gradedpartial",
        "gradedright",
        "mangaveup",
        "mangrwrong",
        "mangrpartial",
        "mangrright",
        "manfinished",
        "finished",
    }
)

#: final states in which Moodle shows no mark (the question was not answered)
NO_MARK_FINAL_STATES: frozenset[str] = frozenset({"gaveup", "mangaveup"})


def is_discussable(qtype: str) -> bool:
    return qtype in DISCUSSABLE_QTYPES

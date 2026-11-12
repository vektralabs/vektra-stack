"""Re-export of the typed read outcome, which lives in ``vektra_shared.lms.types`` (ADR-0033 D2):
the Moodle readers return the same ``Reading`` the ``LmsProvider`` Protocol does."""

from vektra_shared.lms import (
    Readable,
    Reading,
    ShapeEvidence,
    Unreadable,
    UnreadableReason,
)

__all__ = ["Readable", "Reading", "ShapeEvidence", "Unreadable", "UnreadableReason"]

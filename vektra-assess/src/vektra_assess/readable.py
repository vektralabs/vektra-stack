"""Re-export of the typed read outcome, which now lives in ``vektra_shared.lms.types``
(ADR-0033 D2, plan task 2a-1.1).

Kept so the existing imports keep working until plan task 2a-2b deletes this module. The DB-only
``capabilities`` module (importable by the API) uses it without importing the client package
(contract (c)).
"""

from vektra_shared.lms.types import (
    Readable,
    Reading,
    ShapeEvidence,
    Unreadable,
    UnreadableReason,
)

__all__ = ["Readable", "Reading", "ShapeEvidence", "Unreadable", "UnreadableReason"]

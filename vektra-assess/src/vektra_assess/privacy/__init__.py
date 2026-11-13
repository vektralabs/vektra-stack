"""Privacy and retention (plan assess-privacy-retention).

- ``config``: the REQ-089 settings of a site and ``is_configured`` (fail closed);
- ``subject``: the reference a tombstone keeps of its subject;
- ``purge``: the retention purge (crypto-shred first, rows second, then count);
- ``erasure``: the subject and activity erasures (OQ-033).

Two callers depend on ``is_configured``: the START gate BR-006 (h)
(``worker.preflight.privacy_not_configured``) and the teacher's enable (REQ-077 "privacy settings
unset" refuses it, plan assess-teacher-ops). Development and CI may let the Start gate pass with
``VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET``; the enable refusal has no such override.
"""

from __future__ import annotations

from vektra_assess.privacy.config import (
    MAILBOX_SENTENCE,
    PrivacySettings,
    current_notice_version,
    is_configured,
    shown_notice_version,
)

__all__ = [
    "MAILBOX_SENTENCE",
    "PrivacySettings",
    "current_notice_version",
    "is_configured",
    "shown_notice_version",
]

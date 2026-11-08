"""REQ-089 privacy configuration status (plan assess-privacy-retention owns the store).

Until that plan binds the REQ-089 settings store, a site's privacy settings are NOT configured:
``is_configured`` returns False (fail closed). Two callers depend on it: the START gate BR-006 (h)
(``worker.preflight.privacy_not_configured``) and the teacher's enable (REQ-077 "privacy settings
unset" refuses it, plan assess-teacher-ops). Development and CI may let the Start gate pass with
``VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET``; the enable refusal has no such override.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


async def is_configured(_conn: Any, _site_id: UUID) -> bool:
    """Whether the site's REQ-089 privacy settings exist: never, until plan 12 lands."""
    return False

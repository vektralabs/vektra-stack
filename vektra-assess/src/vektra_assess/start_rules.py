"""Start rules shared by the API and the worker (REQ-071, plan 8 M5.4); no worker code here.

System rollbacks are the server-attributable ones: they count towards the 3-consecutive-rollback
notice ("Problema tecnico persistente") and the per-site Start breaker. A gate refusal, a missing
configuration or the admission limit is not a technical problem and never counts.
"""

from __future__ import annotations

SYSTEM_ROLLBACK_REASONS = (
    "start_timeout",
    "capture_failed",
    "part_b_rejected",
    "provider_unavailable",
    "service_misconfigured",
    "internal_error",
    "commit_failed",
)

#: the capture step reports ``capture_failed_<reason>`` (the reader that could not be read):
#: every one of them is a system rollback
CAPTURE_FAILED_PREFIX = "capture_failed"


def is_system_rollback(reason: str | None) -> bool:
    """The one classification, used by the worker (streak, breaker) and mirrored in SQL by
    ``SYSTEM_ROLLBACK_SQL`` (the event stream's persistent-problem check)."""
    return bool(reason) and (
        reason in SYSTEM_ROLLBACK_REASONS
        or str(reason).startswith(CAPTURE_FAILED_PREFIX)
    )


#: the same test in SQL over a column ``reason_code`` (binds ``:sys``)
SYSTEM_ROLLBACK_SQL = (
    "(reason_code = ANY(:sys) OR reason_code LIKE '" + CAPTURE_FAILED_PREFIX + "%')"
)

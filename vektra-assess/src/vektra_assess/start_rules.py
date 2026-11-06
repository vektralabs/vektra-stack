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
    "internal_error",
    "commit_failed",
)

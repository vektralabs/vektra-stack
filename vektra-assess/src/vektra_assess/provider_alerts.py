"""The operator alert of a model provider refusing the configuration (``provider_config_refused``).

An ``auth_error`` or a non-429 ``http_4xx`` (``llm.types.is_config_refusal``) is a refusal, not
an outage: a turn ends with the interrupted marker and a Start rolls back
``provider_unavailable``, with no OUTAGE (REQ-086). That alone is silent for the operator, who is
the only one who can fix a missing key, a wrong model or a rejected parameter. So the first such
refusal queues one ``operator_notify`` job, ``{site_id, kind: provider_config_refused, reason:
<cause>, window: <bucket>}``: ids and closed codes only.

Producers: the API's reply task (a discussion turn) and the worker's Start rollback
(``worker.preflight.rollback_start``), each in its own transaction. Dedupe: one alert per site and
cause per ``WINDOW_S`` (a fixed bucket of the producer's clock in the ``dedupe_key``, like the
``lms_unreachable`` alert). The unique index covers pending jobs only, and the API role cannot
read ``assess_jobs`` to see an alert already sent, so ``delivery.notify`` drops a job whose key
an EARLIER job already carries: one email per site, cause and window, whoever queued it.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from vektra_assess.jobs import JobKind, enqueue_job

KIND = "provider_config_refused"
#: one alert per site and cause per hour, however many turns and Starts are refused in it
WINDOW_S = 3600


def window_of(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // WINDOW_S)


def dedupe_key(site_id: object, cause: str, window: int) -> str:
    return f"operator_notify:{KIND}:{site_id}:{cause}:{window}"


async def queue_provider_config_alert(
    conn: Any, site_id: UUID, cause: str, *, now: float | None = None
) -> bool:
    """Queue the alert in the caller's transaction; False when a pending one covers it."""
    window = window_of(now)
    return await enqueue_job(
        conn,
        JobKind.OPERATOR_NOTIFY,
        {"site_id": str(site_id), "kind": KIND, "reason": cause, "window": window},
        dedupe_key=dedupe_key(site_id, cause, window),
    )

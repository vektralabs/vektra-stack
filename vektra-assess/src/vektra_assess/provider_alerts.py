"""The operator alert of a model provider refusing the configuration (``provider_config_refused``).

An ``auth_error`` or a non-429 ``http_4xx`` (``llm.types.is_config_refusal``) may be the provider
refusing the deployment's configuration, which only the operator can fix (a missing key, a wrong
model, a rejected parameter). It is CONFIRMED as such (``llm.probe.refusal_is_configuration``)
when it is an ``auth_error``, or when the turn-shaped probe is refused as well; a 4xx the probe
does not get is that one request's (a context-length or content-policy 400) and keeps the plain
refusal: the marked turn, ``provider_refused``, the session ACTIVE (REQ-086). What the student
gets for a CONFIRMED one (live E2E follow-up, 2026-11-13; review of #221):

- a discussion turn ends with the interrupted marker, its ``reply_interrupted`` event carries
  ``reason: service_misconfigured``, and in the marker's own transaction the session enters
  OUTAGE with that same reason (the open interval's ``reason_code``): the timer is frozen, the
  outage probe keeps checking the provider (a refused probe never ends the outage), and once
  the configuration is fixed the probe ends it and the interval is credited exactly like a
  provider outage (G, C and the 3-consecutive-failures rule included). The turn is NOT replayed;
- a Start rolls back ``service_misconfigured`` (a system rollback, like ``provider_unavailable``
  before it).

That alone is silent for the operator. So the first config refusal (confirmed or not, as
before) queues one ``operator_notify``
job, ``{site_id, kind: provider_config_refused, reason: <cause>, window: <bucket>}``: ids and
closed codes only. A session in OUTAGE takes no turn, so it produces no further refusal.

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

#: the closed reason a configuration refusal shows the student: the ``reply_interrupted`` event,
#: the outage interval's ``reason_code`` (so ``outage_start`` and the session snapshot carry it)
#: and the Start rollback reason
SERVICE_MISCONFIGURED = "service_misconfigured"
#: the reason of every other outage (the provider unreachable, timing out, failing)
PROVIDER_UNAVAILABLE = "provider_unavailable"


def outage_reason(reason_code: object) -> str:
    """The student-facing reason of an open outage interval, from its ``reason_code``: a
    closed pair, never the stored value itself (older intervals carry the call outcome)."""
    return (
        SERVICE_MISCONFIGURED
        if reason_code == SERVICE_MISCONFIGURED
        else PROVIDER_UNAVAILABLE
    )


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

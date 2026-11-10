"""The attempt outcome shared by the transcript and notification handlers (plan 13a).

Both handlers claim a row, send once, then settle it (OQ-029). Before this module each one decided
the outcome and wrote the attempt's audit rows with its own copy of the same code (deferred from
the review of PR #190). The two copies differ only in where a retry's ``not_before`` comes from
and in the event names, so those are the parameters here. The claim stays in each handler: its
statement and its Message-ID are specific to the row kind.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from vektra_assess.delivery.smtp import SendResult

Outcome = Literal["sent", "failed", "retrying"]


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    #: the error class or the handler's own reason code; None for a clean send
    cls: str | None
    #: when a retry is due (the transcript handler's schedule); None for the notification
    #: handler, whose next run carries the schedule
    not_before: datetime | None = None


def decide(
    result: SendResult,
    reason: str | None,
    *,
    final: bool,
    next_retry: Callable[[], datetime | None] | None = None,
) -> Decision:
    """What one attempt amounts to.

    - a 250 is ``sent``;
    - a final reason or a non-retryable error class is ``failed``;
    - otherwise ``retrying``, unless ``next_retry`` returns None (the retry window is spent),
      which makes it ``failed``.
    """
    cls = reason or (result.error_class.value if result.error_class else None)
    if result.ok:
        return Decision("sent", cls)
    if final or (result.error_class is not None and not result.retryable):
        return Decision("failed", cls)
    if next_retry is None:
        return Decision("retrying", cls)
    nb = next_retry()
    return Decision("failed", cls) if nb is None else Decision("retrying", cls, nb)


@dataclass(frozen=True)
class AttemptEvents:
    attempt: str
    sent: str
    failed: str


async def record(
    audit: Callable[..., Awaitable[None]],
    conn: Any,
    events: AttemptEvents,
    d: Decision,
    *,
    attempt: int,
    smtp_code: int | None,
    duplicate: bool,
    ids: dict[str, Any],
) -> None:
    """The attempt row, then SENT or FAILED: the same three events for both kinds (REQ-085)."""
    await audit(
        conn,
        events.attempt,
        reason=d.cls,
        attempt=attempt,
        outcome=d.outcome,
        smtp_code=smtp_code,
        duplicate=duplicate,
        **ids,
    )
    if d.outcome == "sent":
        await audit(conn, events.sent, **ids)
    elif d.outcome == "failed":
        await audit(conn, events.failed, reason=d.cls, **ids)


def count(out: Any, outcome: Outcome) -> None:
    """Both result records keep ``sent`` / ``failed`` / ``retrying`` counters."""
    setattr(out, outcome, getattr(out, outcome) + 1)

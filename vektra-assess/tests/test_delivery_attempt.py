"""The shared attempt outcome of the transcript and notification handlers (plan 13a)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from vektra_assess.delivery.attempt import AttemptEvents, count, decide, record
from vektra_assess.delivery.smtp import ErrorClass, SendResult

NB = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _retryable() -> SendResult:
    return SendResult(False, ErrorClass.TEMP_4XX, 451)


def test_a_250_is_sent() -> None:
    d = decide(SendResult(True, None, 250), None, final=False)
    assert (d.outcome, d.cls) == ("sent", None)


def test_a_final_reason_fails_even_when_the_error_is_retryable() -> None:
    d = decide(_retryable(), "domain_not_allowed", final=True, next_retry=lambda: NB)
    assert (d.outcome, d.cls) == ("failed", "domain_not_allowed")


def test_a_permanent_error_fails_without_asking_the_schedule() -> None:
    asked: list[bool] = []

    def nr() -> datetime:
        asked.append(True)
        return NB

    d = decide(
        SendResult(False, ErrorClass.PERM_5XX, 550), None, final=False, next_retry=nr
    )
    assert d.outcome == "failed" and asked == []


def test_a_retryable_error_retries_at_the_scheduled_time() -> None:
    d = decide(_retryable(), None, final=False, next_retry=lambda: NB)
    assert (d.outcome, d.not_before) == ("retrying", NB)


def test_a_spent_window_turns_the_retry_into_a_failure() -> None:
    d = decide(_retryable(), None, final=False, next_retry=lambda: None)
    assert d.outcome == "failed"


def test_without_a_schedule_a_retryable_error_retries() -> None:
    """The notification handler: its next run carries the schedule."""
    d = decide(_retryable(), None, final=False)
    assert (d.outcome, d.not_before) == ("retrying", None)


async def test_record_writes_the_attempt_then_the_outcome_event() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def audit(_conn: Any, event: str, **kw: Any) -> None:
        calls.append((event, kw))

    ev = AttemptEvents("a", "s", "f")
    ids = {"delivery_id": "x"}
    for result, expected in (
        (SendResult(True, None, 250), ["a", "s"]),
        (SendResult(False, ErrorClass.PERM_5XX, 550), ["a", "f"]),
        (_retryable(), ["a"]),
    ):
        calls.clear()
        d = decide(result, None, final=False)
        await record(
            audit,
            None,
            ev,
            d,
            attempt=1,
            smtp_code=result.smtp_code,
            duplicate=False,
            ids=ids,
        )
        assert [c[0] for c in calls] == expected
        assert calls[0][1]["outcome"] == d.outcome and calls[0][1]["delivery_id"] == "x"


def test_count_increments_the_matching_counter() -> None:
    class Out:
        sent = failed = retrying = 0

    o = Out()
    count(o, "sent")
    count(o, "retrying")
    count(o, "retrying")
    assert (o.sent, o.failed, o.retrying) == (1, 0, 2)

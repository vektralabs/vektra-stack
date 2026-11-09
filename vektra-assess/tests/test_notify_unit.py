"""``teacher_notify`` / ``operator_notify`` without a database: the event a job describes, the
email it becomes, and the refusal of anything else (the end-to-end runs are in
``integration/test_notify_handler.py``)."""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from vektra_assess import audit, jobs
from vektra_assess.delivery import notify as n
from vektra_assess.delivery.backoff import parse_schedule
from vektra_assess.delivery.recipients import recipient_ref
from vektra_assess.delivery.smtp import ErrorClass, SendResult, SlotBusy
from vektra_assess.jobs import JobKind

SITE = uuid.UUID("11111111-1111-1111-1111-111111111111")
SID = uuid.UUID("22222222-2222-2222-2222-222222222222")
WHEN = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row


class Conn:
    """Answers the one session lookup ``read_event`` makes."""

    def __init__(self, row: Any = None) -> None:
        self.row = row
        self.calls = 0

    async def execute(self, *_: Any) -> _Result:
        self.calls += 1
        return _Result(self.row)


SESSION_ROW = SimpleNamespace(site_id=SITE, cmid=12, student_userid=345, session_no=2)


async def test_a_service_failure_job_is_re_read_from_its_session() -> None:
    conn = Conn(SESSION_ROW)
    e = await n.read_event(
        conn,
        JobKind.TEACHER_NOTIFY,
        {"session_id": str(SID), "reason": "service_failure"},
    )
    assert e == n.Event(
        "service_failure",
        SITE,
        cmid=12,
        student_userid=345,
        session_id=SID,
        session_no=2,
    )


@pytest.mark.parametrize(
    ("kind", "args", "row"),
    [
        # the session is gone (purged): nothing to point the teacher to
        (
            JobKind.TEACHER_NOTIFY,
            {"session_id": str(SID), "reason": "service_failure"},
            None,
        ),
        # a session job for the operator, or with another reason
        (
            JobKind.OPERATOR_NOTIFY,
            {"session_id": str(SID), "reason": "service_failure"},
            1,
        ),
        (JobKind.TEACHER_NOTIFY, {"session_id": str(SID), "reason": "finished"}, 1),
        (
            JobKind.TEACHER_NOTIFY,
            {"session_id": "not-a-uuid", "reason": "service_failure"},
            1,
        ),
        # no site, a bad site
        (JobKind.OPERATOR_NOTIFY, {"reason": "rollback_cap"}, None),
        (JobKind.OPERATOR_NOTIFY, {"site_id": "x", "reason": "rollback_cap"}, None),
        # the LMS alert is the operator's only, with a fixed reason word
        (
            JobKind.TEACHER_NOTIFY,
            {
                "site_id": str(SITE),
                "reason": "unreadable",
                "kind": "moodle_unreachable",
            },
            None,
        ),
        (
            JobKind.OPERATOR_NOTIFY,
            {
                "site_id": str(SITE),
                "reason": "Robert'); DROP",
                "kind": "moodle_unreachable",
            },
            None,
        ),
        (
            JobKind.OPERATOR_NOTIFY,
            {"site_id": str(SITE), "reason": 5, "kind": "moodle_unreachable"},
            None,
        ),
        # a rollback cap without its ids, or with ids that are not ASCII digits
        (
            JobKind.TEACHER_NOTIFY,
            {"site_id": str(SITE), "reason": "rollback_cap", "cmid": 3},
            None,
        ),
        (
            JobKind.TEACHER_NOTIFY,
            {
                "site_id": str(SITE),
                "reason": "rollback_cap",
                "cmid": "٣",
                "student_userid": 4,
            },
            None,
        ),
        (
            JobKind.TEACHER_NOTIFY,
            {
                "site_id": str(SITE),
                "reason": "rollback_cap",
                "cmid": True,
                "student_userid": 4,
            },
            None,
        ),
        # an unknown event
        (JobKind.TEACHER_NOTIFY, {"site_id": str(SITE), "reason": "other"}, None),
    ],
)
async def test_a_malformed_or_stale_job_is_a_no_op(
    kind: JobKind, args: dict[str, Any], row: Any
) -> None:
    conn = Conn(SESSION_ROW if row == 1 else row)
    assert await n.read_event(conn, kind, args) is None


async def test_the_producers_argument_shapes_are_read() -> None:
    cap = await n.read_event(
        Conn(),
        JobKind.OPERATOR_NOTIFY,
        {
            "site_id": str(SITE),
            "cmid": 12,
            "student_userid": "345",
            "reason": "rollback_cap",
        },
    )
    assert cap == n.Event("rollback_cap", SITE, cmid=12, student_userid=345)
    lms = await n.read_event(
        Conn(),
        JobKind.OPERATOR_NOTIFY,
        {"site_id": str(SITE), "reason": "unreadable", "kind": "moodle_unreachable"},
    )
    assert lms == n.Event("moodle_unreachable", SITE, reason="unreadable")


def _mail(kind: JobKind, e: n.Event) -> Any:
    return n.build(
        kind,
        e,
        sender="vektra@vektra.example.org",
        recipient="prof@uni.example",
        message_id="<abc@vektra.example.org>",
        date=WHEN,
    )


def test_the_teacher_service_failure_email_names_ids_only() -> None:
    e = n.Event(
        "service_failure",
        SITE,
        cmid=12,
        student_userid=345,
        session_id=SID,
        session_no=2,
    )
    msg = _mail(JobKind.TEACHER_NOTIFY, e)
    body = msg.get_content()
    assert "attivita': 12" in body and "(id Moodle): 345" in body and "#2" in body
    assert msg["Subject"].startswith("Discussione chiusa per un guasto del servizio")
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg["Message-ID"] == "<abc@vektra.example.org>"
    assert msg["To"] == "prof@uni.example"
    assert not list(msg.iter_attachments())


def test_the_teacher_rollback_email_and_the_operator_ones() -> None:
    cap = n.Event("rollback_cap", SITE, cmid=12, student_userid=345)
    teacher = _mail(JobKind.TEACHER_NOTIFY, cap).get_content()
    assert "345" in teacher and "piu' volte di seguito" in teacher
    ops = _mail(JobKind.OPERATOR_NOTIFY, cap)
    assert str(SITE) in ops.get_content() and "345" not in ops.get_content()
    assert "345" not in str(ops["Subject"])
    lms = _mail(
        JobKind.OPERATOR_NOTIFY,
        n.Event("moodle_unreachable", SITE, reason="unreadable"),
    )
    assert "motivo: unreadable" in lms.get_content()


def test_there_is_no_text_for_an_event_a_kind_never_carries() -> None:
    with pytest.raises(ValueError, match="no text"):
        _mail(JobKind.TEACHER_NOTIFY, n.Event("moodle_unreachable", SITE, reason="x"))


def test_a_header_value_cannot_inject_a_header() -> None:
    msg = n.build(
        JobKind.TEACHER_NOTIFY,
        n.Event("rollback_cap", SITE, cmid=1, student_userid=2),
        sender="vektra@vektra.example.org",
        recipient="prof@uni.example\r\nBcc: x@evil.example",
        message_id="<a@b>",
        date=WHEN,
    )
    assert msg["Bcc"] is None


def test_the_teacher_ref_is_the_transcript_rows_and_the_operator_ref_is_site_free() -> (
    None
):
    a = "prof@uni.example"
    assert n.notify_ref(JobKind.TEACHER_NOTIFY, SITE, a) == recipient_ref(SITE, a)
    other = uuid.uuid4()
    assert n.notify_ref(JobKind.OPERATOR_NOTIFY, SITE, a) == n.notify_ref(
        JobKind.OPERATOR_NOTIFY, other, a
    )


def test_a_handler_exists_only_for_the_two_notification_kinds() -> None:
    deps: Any = SimpleNamespace()
    for kind in n.KINDS:
        assert callable(n.make_notify_handler(deps, kind))
    with pytest.raises(ValueError, match="not a notification kind"):
        n.make_notify_handler(deps, JobKind.PURGE)


def test_a_chain_run_keeps_its_root_try_and_start() -> None:
    e = n.Event("rollback_cap", SITE, cmid=1, student_userid=2)
    first = n._run(JobKind.TEACHER_NOTIFY, SimpleNamespace(job_id=9, args={}), e, WHEN)
    assert (first.root, first.try_no, first.since, first.job_id) == (9, 1, WHEN, 9)
    since = int(WHEN.timestamp()) - 60
    later = n._run(
        JobKind.TEACHER_NOTIFY,
        SimpleNamespace(job_id=15, args={"root_job_id": 9, "try": 3, "since": since}),
        e,
        WHEN,
    )
    assert (later.root, later.try_no, later.job_id) == (9, 3, 15)
    assert later.since == datetime.fromtimestamp(since, UTC)


# --- the run, over an in-memory outbox ---------------------------------------------------------


class _Rows:
    def __init__(self, rows: list[Any], rowcount: int | None = None) -> None:
        self._rows = rows
        self.rowcount = len(rows) if rowcount is None else rowcount

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class Outbox:
    """The ``assess_deliveries`` rows, the audit rows and the queued jobs a run writes, answered
    statement by statement (the SQL itself is exercised by the integration tests)."""

    def __init__(self, clock: Any) -> None:
        self.rows: list[dict[str, Any]] = []
        self.audit: list[tuple[str, str | None]] = []
        self.jobs: list[dict[str, Any]] = []
        self.clock = clock

    def _row(self, i: Any) -> dict[str, Any] | None:
        return next((r for r in self.rows if r["id"] == i), None)

    @staticmethod
    def _ns(r: dict[str, Any], **extra: Any) -> SimpleNamespace:
        return SimpleNamespace(**r, **extra)

    async def execute(self, stmt: Any, p: Any = None) -> _Rows:
        p = p or {}
        if stmt is n._ROWS:
            return _Rows([self._ns(r) for r in self.rows if r["job_id"] == p["j"]])
        if stmt is n._ADD:
            if any(
                r["job_id"] == p["j"] and r["recipient_ref"] == p["ref"]
                for r in self.rows
            ):
                return _Rows([], 0)
            self.rows.append(
                {
                    "id": uuid.uuid4(),
                    "job_id": p["j"],
                    "recipient_ref": p["ref"],
                    "status": "PENDING",
                    "attempt_started_at": None,
                    "attempts": 0,
                    "message_id": None,
                    "cls": None,
                }
            )
            return _Rows([], 1)
        if stmt is n._WITHDRAW:
            r = self._row(p["i"])
            if r is None or r["status"] not in n.LIVE:
                return _Rows([])
            prev = r["attempt_started_at"]
            r.update(status="FAILED", cls=p["cls"], attempt_started_at=None)
            return _Rows(
                [
                    SimpleNamespace(
                        id=r["id"],
                        recipient_ref=r["recipient_ref"],
                        attempt_started_at=prev,
                    )
                ]
            )
        if stmt is n._CLAIM:
            r = self._row(p["i"])
            if r is None or r["status"] not in n.LIVE:
                return _Rows([])
            prev = r["attempt_started_at"]
            if prev is not None and prev >= p["stale"]:
                return _Rows([])  # still inside its lease
            r["attempt_started_at"] = p["now"]
            r["attempts"] += 1
            r["message_id"] = r["message_id"] or p["mid"]
            return _Rows([self._ns(r, prev=prev)])
        if stmt in (n._SENT, n._RETRY, n._FAIL):
            r = self._row(p["i"])
            if r is None or r["attempt_started_at"] != p["started"]:
                return _Rows([], 0)
            status = {n._SENT: "SENT", n._RETRY: "RETRYING", n._FAIL: "FAILED"}[stmt]
            r.update(status=status, cls=p.get("cls"), attempt_started_at=None)
            return _Rows([], 1)
        if stmt is n._LIVE_ROWS:
            return _Rows([], 0)
        if stmt is audit._INSERT:
            self.audit.append((p["event"], p["reason"]))
            return _Rows([], 1)
        if stmt is jobs._INSERT:
            self.jobs.append(p)
            return _Rows([], 1)
        if stmt is jobs._NOTIFY:
            return _Rows([], 1)
        raise AssertionError(f"unexpected statement {stmt}")

    async def rollback(self) -> None:
        return None

    @contextlib.asynccontextmanager
    async def begin(self) -> Any:
        yield self

    @contextlib.asynccontextmanager
    async def acquire(self) -> Any:
        yield self


class Recipients:
    def __init__(self, found: tuple[str, ...] | None) -> None:
        self.found = found

    async def read(self, *_: Any) -> tuple[str, ...] | None:
        return self.found


class Sender:
    def __init__(self, *results: SendResult | type[BaseException]) -> None:
        self.results = list(results)
        self.sent: list[tuple[str, str]] = []

    @contextlib.asynccontextmanager
    async def slot(self) -> Any:
        if self.results and self.results[0] is SlotBusy:
            self.results.pop(0)
            raise SlotBusy
        yield

    async def send(
        self, msg: Any, recipient: str, *, slot_held: bool = False
    ) -> SendResult:
        self.sent.append((recipient, str(msg["Message-ID"])))
        return self.results.pop(0) if self.results else SendResult(True, smtp_code=250)


class Clock:
    def __init__(self) -> None:
        self.now = WHEN

    def __call__(self) -> datetime:
        return self.now


def _deps(
    found: tuple[str, ...] | None, sender: Sender, clock: Clock, **kw: Any
) -> n.NotifyDeps:
    base: dict[str, Any] = {
        "recipients": Recipients(found),
        "sender": sender,
        "sender_address": "vektra@vektra.example.org",
        "message_id_domain": "vektra.example.org",
        "domains": frozenset({"uni.example"}),
        "operator_address": "ops@vektra.example.org",
        "schedule": parse_schedule("1m,5m"),
        "window": timedelta(hours=1),
        "clock": clock,
    }
    return n.NotifyDeps(**{**base, **kw})


CAP = {
    "site_id": str(SITE),
    "cmid": 12,
    "student_userid": 345,
    "reason": "rollback_cap",
}


def _ctx(box: Outbox, job_id: int = 9, args: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(
        job_id=job_id, args=args or CAP, attempt=1, acquire=box.acquire
    )


def _events(box: Outbox) -> list[str]:
    return [e for e, _ in box.audit]


async def test_every_configured_teacher_is_mailed_once_and_audited() -> None:
    clock = Clock()
    box = Outbox(clock)
    sender = Sender()
    deps = _deps(("a@uni.example", "b@uni.example"), sender, clock)
    got = await n.notify(deps, JobKind.TEACHER_NOTIFY, _ctx(box))
    assert got is not None and got.sent == 2 and got.next_run is None
    assert sorted(a for a, _ in sender.sent) == ["a@uni.example", "b@uni.example"]
    assert _events(box).count("assess_notify_sent") == 2
    again = await n.notify(deps, JobKind.TEACHER_NOTIFY, _ctx(box))
    assert again is not None and again.sent == 0 and len(sender.sent) == 2


async def test_a_retryable_class_chains_the_next_run_and_a_final_one_fails() -> None:
    clock = Clock()
    box = Outbox(clock)
    sender = Sender(
        SendResult(False, ErrorClass.CONNECT),
        SendResult(False, ErrorClass.PERM_5XX, 550),
    )
    deps = _deps(("a@uni.example", "b@uni.example"), sender, clock)
    got = await n.notify(deps, JobKind.TEACHER_NOTIFY, _ctx(box))
    assert got is not None and got.retrying == 1 and got.failed == 1
    assert got.next_run == WHEN + timedelta(minutes=1)
    (nxt,) = box.jobs
    assert nxt["dedupe_key"] == "teacher_notify:next:9:9"
    assert '"root_job_id":9' in nxt["args"] and '"try":2' in nxt["args"]


async def test_a_tls_failure_alerts_and_an_outside_domain_is_never_sent() -> None:
    clock = Clock()
    box = Outbox(clock)
    sender = Sender(SendResult(False, ErrorClass.TLS))
    deps = _deps(("a@uni.example", "x@elsewhere.example"), sender, clock)
    got = await n.notify(deps, JobKind.TEACHER_NOTIFY, _ctx(box))
    assert got is not None and got.failed == 1 and got.retrying == 1
    assert [a for a, _ in sender.sent] == ["a@uni.example"]
    assert ("assess_notify_failed", "domain_not_allowed") in box.audit


async def test_a_busy_relay_retries_soon_without_an_attempt() -> None:
    clock = Clock()
    box = Outbox(clock)
    got = await n.notify(
        _deps(("a@uni.example",), Sender(SlotBusy), clock),
        JobKind.TEACHER_NOTIFY,
        _ctx(box),
    )
    assert got is not None and got.busy == 1 and got.next_run == WHEN + n.BUSY_RETRY
    assert box.rows[0]["attempts"] == 0


async def test_a_crash_marker_is_resent_with_its_message_id_and_audited() -> None:
    clock = Clock()
    box = Outbox(clock)
    deps = _deps(
        ("a@uni.example",), Sender(SendResult(False, ErrorClass.CONNECT)), clock
    )
    await n.notify(deps, JobKind.TEACHER_NOTIFY, _ctx(box))
    # an attempt with no outcome, older than the lease: a crash
    box.rows[0]["attempt_started_at"] = WHEN - timedelta(minutes=5)
    sender = Sender()
    got = await n.notify(
        _deps(("a@uni.example",), sender, clock), JobKind.TEACHER_NOTIFY, _ctx(box)
    )
    assert got is not None and got.duplicates == 1 and got.sent == 1
    assert sender.sent[0][1] == box.rows[0]["message_id"]
    assert "assess_notify_possible_duplicate" in _events(box)


async def test_a_withdrawn_recipient_fails_and_the_window_ends_the_chain() -> None:
    clock = Clock()
    box = Outbox(clock)
    deps = _deps(
        ("a@uni.example",), Sender(SendResult(False, ErrorClass.CONNECT)), clock
    )
    await n.notify(deps, JobKind.TEACHER_NOTIFY, _ctx(box))
    # the teacher replaced the address: the old row is FAILED, the new one is mailed
    sender = Sender()
    got = await n.notify(
        _deps(("c@uni.example",), sender, clock), JobKind.TEACHER_NOTIFY, _ctx(box)
    )
    assert got is not None and got.sent == 1
    assert ("assess_notify_failed", "recipient_withdrawn") in box.audit
    # past the window, a row still live fails as window_expired
    box2 = Outbox(clock)
    deps = _deps(
        ("a@uni.example",), Sender(SendResult(False, ErrorClass.CONNECT)), clock
    )
    late = {
        **CAP,
        "root_job_id": 9,
        "try": 4,
        "since": int((WHEN - timedelta(hours=2)).timestamp()),
    }
    got = await n.notify(deps, JobKind.TEACHER_NOTIFY, _ctx(box2, 30, late))
    assert got is not None and got.next_run is None
    assert ("assess_notify_failed", "window_expired") in box2.audit


async def test_nobody_configured_and_unreadable_recipients() -> None:
    clock = Clock()
    box = Outbox(clock)
    got = await n.notify(_deps((), Sender(), clock), JobKind.TEACHER_NOTIFY, _ctx(box))
    assert got is not None and got.failed == 1
    assert box.audit == [("assess_notify_failed", "no_recipients")]
    box = Outbox(clock)
    got = await n.notify(
        _deps(None, Sender(), clock), JobKind.TEACHER_NOTIFY, _ctx(box)
    )
    assert got is not None and got.unresolved and got.next_run is not None
    # unreadable past the window: the notification itself fails
    box = Outbox(clock)
    late = {**CAP, "try": 9, "since": int((WHEN - timedelta(hours=2)).timestamp())}
    got = await n.notify(
        _deps(None, Sender(), clock), JobKind.TEACHER_NOTIFY, _ctx(box, 31, late)
    )
    assert got is not None and got.next_run is None
    assert box.audit == [("assess_notify_failed", "recipients_unresolved")]


async def test_the_operator_is_mailed_at_its_address_without_the_allowlist() -> None:
    clock = Clock()
    box = Outbox(clock)
    sender = Sender()
    deps = _deps(None, sender, clock, domains=frozenset())
    got = await n.notify(deps, JobKind.OPERATOR_NOTIFY, _ctx(box))
    assert got is not None and got.sent == 1
    assert sender.sent[0][0] == "ops@vektra.example.org"
    handler = n.make_notify_handler(deps, JobKind.OPERATOR_NOTIFY)
    await handler(_ctx(Outbox(clock), 10))
    # a stale job completes as a no-op
    assert (
        await n.notify(deps, JobKind.OPERATOR_NOTIFY, _ctx(box, 11, {"reason": "x"}))
        is None
    )


async def test_replaced_recipients_free_their_slots() -> None:
    """A teacher who replaced every address between two runs: the old rows fail as withdrawn
    and do not count towards the cap of three, so the new addresses are all mailed."""
    clock = Clock()
    box = Outbox(clock)
    old = ("a@uni.example", "b@uni.example", "c@uni.example")
    down = Sender(*(SendResult(False, ErrorClass.CONNECT) for _ in old))
    await n.notify(_deps(old, down, clock), JobKind.TEACHER_NOTIFY, _ctx(box))
    new = ("d@uni.example", "e@uni.example", "f@uni.example")
    sender = Sender()
    got = await n.notify(_deps(new, sender, clock), JobKind.TEACHER_NOTIFY, _ctx(box))
    assert got is not None and got.sent == 3
    assert sorted(a for a, _ in sender.sent) == list(new)


# --- code review (2026-11-09), each failing on the first version -------------------------------


async def test_an_attempt_still_inside_the_lease_is_never_taken_again() -> None:
    """Review #1: a retried run must not claim a row whose send may still be running."""
    clock = Clock()
    box = Outbox(clock)
    deps = _deps(
        ("a@uni.example",), Sender(SendResult(False, ErrorClass.CONNECT)), clock
    )
    await n.notify(deps, JobKind.TEACHER_NOTIFY, _ctx(box))
    box.rows[0]["attempt_started_at"] = WHEN  # in flight, well inside the lease
    sender = Sender()
    got = await n.notify(
        _deps(("a@uni.example",), sender, clock), JobKind.TEACHER_NOTIFY, _ctx(box)
    )
    assert got is not None and sender.sent == [] and got.duplicates == 0
    assert box.rows[0]["attempt_started_at"] == WHEN


class Boom(Sender):
    async def send(
        self, msg: Any, recipient: str, *, slot_held: bool = False
    ) -> SendResult:
        if recipient.startswith("a@"):
            raise RuntimeError("relay library bug")
        return await super().send(msg, recipient, slot_held=slot_held)


async def test_a_run_that_raises_still_finishes_its_rows_and_queues_the_next() -> None:
    """Review #1/#2: one row's exception neither orphans the other sends nor kills the chain
    (a dead job would leave the rows live forever, with no alert)."""
    clock = Clock()
    box = Outbox(clock)
    sender = Boom()
    deps = _deps(("a@uni.example", "b@uni.example"), sender, clock)
    got = await n.notify(deps, JobKind.TEACHER_NOTIFY, _ctx(box))
    assert got is not None and got.sent == 1 and got.next_run is not None
    assert [a for a, _ in sender.sent] == ["b@uni.example"]
    assert len(box.jobs) == 1


async def test_a_busy_relay_does_not_advance_the_backoff() -> None:
    """Review #3: a busy-only rerun keeps its try number."""
    clock = Clock()
    box = Outbox(clock)
    await n.notify(
        _deps(("a@uni.example",), Sender(SlotBusy), clock),
        JobKind.TEACHER_NOTIFY,
        _ctx(box),
    )
    (nxt,) = box.jobs
    assert '"try":1' in nxt["args"]


async def test_a_crash_marked_row_whose_recipient_left_audits_the_possible_duplicate() -> (
    None
):
    """Review #6: the relay may hold the mail; the withdrawal must not erase that."""
    clock = Clock()
    box = Outbox(clock)
    deps = _deps(
        ("a@uni.example",), Sender(SendResult(False, ErrorClass.CONNECT)), clock
    )
    await n.notify(deps, JobKind.TEACHER_NOTIFY, _ctx(box))
    box.rows[0]["attempt_started_at"] = WHEN - timedelta(
        hours=1
    )  # a crash, past the lease
    got = await n.notify(
        _deps(("z@uni.example",), Sender(), clock), JobKind.TEACHER_NOTIFY, _ctx(box)
    )
    assert got is not None and got.duplicates == 1
    assert "assess_notify_possible_duplicate" in _events(box)
    assert ("assess_notify_failed", "recipient_withdrawn") in box.audit


class Changing(Recipients):
    def __init__(self, *answers: tuple[str, ...]) -> None:
        super().__init__(answers[0])
        self.answers = list(answers)

    async def read(self, *_: Any) -> tuple[str, ...] | None:
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


async def test_an_address_removed_while_waiting_for_the_relay_is_not_mailed() -> None:
    """Review #7: the address must still be configured once the relay session is held."""
    clock = Clock()
    box = Outbox(clock)
    sender = Sender()
    deps = _deps(None, sender, clock, recipients=Changing(("a@uni.example",), ()))
    got = await n.notify(deps, JobKind.TEACHER_NOTIFY, _ctx(box))
    assert got is not None and sender.sent == [] and got.next_run is not None

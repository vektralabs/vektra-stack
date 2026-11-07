"""The production slot table: every job kind and tick the worker knows, with owner and schedule.

A *slot* is a named place for work: one per job kind (``HandlerSpec``) and one per tick
(``TickSpec``). This plan registers **all** of them with their schedule, budget and lease, but
writes only the framework bodies (heartbeat, done-job cleanup); the rest are bound by the plan that
owns them (``OWNERS``). Until bound, a slot raises ``HandlerNotBound``, and ``assert_all_bound``
makes the worker **refuse to start** while assess is enabled: a missing handler must never be a
silent no-op that completes jobs. Binding a name that is not in the table is rejected too, so a test
or chaos handler cannot slip into the production composition; the chaos harness composes its own
runtime instead.

Schedules marked "design default" in the ownership table have no figure in the architecture; the
values here are conservative and the owning plan may change them with its body.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from vektra_assess.config import VektraAssessSettings
from vektra_assess.jobs import JobKind
from vektra_assess.worker import locks
from vektra_assess.worker.policy import WorkerPolicy
from vektra_assess.worker.runner import Handler, HandlerSpec, TickBody, TickSpec

SESSION = "20261101-154950-assess-session-lifecycle"
TRANSCRIPT = "20261101-154950-assess-transcript-delivery"
PRIVACY = "20261101-154950-assess-privacy-retention"
MOODLE_WS = "20261101-154950-assess-moodle-ws"
JOB_LOOP = "20261101-154950-assess-job-loop"
HARDENING = "20261101-154950-assess-hardening-release"

# job kind -> plan that binds its handler
HANDLER_OWNERS: dict[JobKind, str] = {
    JobKind.PREFLIGHT: SESSION,
    JobKind.RECONCILE_REPLY: SESSION,
    JobKind.SESSION_DEADLINE: SESSION,
    JobKind.ENROLMENT_RECHECK: SESSION,
    JobKind.GATE_REFRESH: SESSION,
    JobKind.RENDER_CHECK: SESSION,
    JobKind.TRANSCRIPT_GENERATE: TRANSCRIPT,
    JobKind.TRANSCRIPT_DELIVER: TRANSCRIPT,
    JobKind.TEACHER_NOTIFY: TRANSCRIPT,
    JobKind.OPERATOR_NOTIFY: TRANSCRIPT,
    JobKind.PURGE: PRIVACY,
    JobKind.ERASURE: PRIVACY,
    JobKind.SETUP_CHECK: MOODLE_WS,
    JobKind.CAPABILITY_PROBE: MOODLE_WS,
    JobKind.ROTATE_KEYS: HARDENING,
}

# tick name -> plan that binds its body
TICK_OWNERS: dict[str, str] = {
    "heartbeat": JOB_LOOP,
    "job_cleanup": JOB_LOOP,
    "lease_reaper": JOB_LOOP,
    "sweeper": SESSION,
    "starting_reaper": SESSION,
    "outage_probe": SESSION,
    "enrolment_recheck": SESSION,
    "gate_refresh": SESSION,
    "delivery_retry": TRANSCRIPT,
    "smtp_probe": TRANSCRIPT,
    "purge": PRIVACY,
    "setup_check": MOODLE_WS,
}


class SlotsNotBound(RuntimeError):
    """One or more production slots have no body; the worker refuses to start."""


@dataclass(frozen=True, slots=True)
class Bindings:
    """Bodies supplied by the composition root. Names outside the slot table are rejected."""

    handlers: Mapping[JobKind, Handler]
    ticks: Mapping[str, TickBody]


@dataclass(frozen=True, slots=True)
class Slots:
    handlers: tuple[HandlerSpec, ...]
    ticks: tuple[TickSpec, ...]

    def unbound(self) -> list[str]:
        out = [f"job:{h.kind.value}" for h in self.handlers if not h.bound]
        return out + [f"tick:{t.name}" for t in self.ticks if not t.bound]


def _tick_table(s: VektraAssessSettings) -> list[TickSpec]:
    def t(
        name: str,
        interval: float,
        timeout: float,
        key: int,
        *,
        jitter_s: float = 0.0,
        run_on_startup: bool = False,
    ) -> TickSpec:
        return TickSpec(
            name,
            interval_s=interval,
            timeout_s=timeout,
            lock_key=key,
            jitter_s=jitter_s,
            run_on_startup=run_on_startup,
        )

    return [
        t("heartbeat", s.worker_heartbeat_interval_s, 5, locks.HEARTBEAT),
        t("job_cleanup", 3600, 60, locks.JOB_CLEANUP, jitter_s=300),
        t("lease_reaper", 60, 30, locks.LEASE_REAPER, jitter_s=10),
        t("sweeper", 10, 20, locks.SWEEPER, jitter_s=5),  # 10-15 s (ownership table)
        t("starting_reaper", 15, 20, locks.STARTING_REAPER, jitter_s=3),
        t("outage_probe", 5, 10, locks.OUTAGE_PROBE, jitter_s=4),  # 5-10 s per provider
        t("enrolment_recheck", 60, 40, locks.ENROLMENT_RECHECK, jitter_s=10),
        t("gate_refresh", 300, 60, locks.GATE_REFRESH, jitter_s=30),  # design default
        t("delivery_retry", 30, 30, locks.DELIVERY_RETRY, jitter_s=5),  # design default
        # INT-003 readiness of the relay for /health/assess; never sends (design default)
        t("smtp_probe", 300, 45, locks.SMTP_PROBE, jitter_s=30, run_on_startup=True),
        t("purge", 86_400, 600, locks.PURGE, jitter_s=3600),  # daily
        t(
            "setup_check",
            86_400,
            120,
            locks.SETUP_CHECK,
            jitter_s=3600,
            run_on_startup=True,
        ),
    ]


def build_slots(
    policy: WorkerPolicy,
    settings: VektraAssessSettings,
    bindings: Bindings | None = None,
) -> Slots:
    """Every production slot, bound where ``bindings`` supplies a body, unbound otherwise."""
    b = bindings or Bindings({}, {})
    extra_kinds = set(b.handlers) - set(HANDLER_OWNERS)
    ticks = _tick_table(settings)
    extra_ticks = set(b.ticks) - {t.name for t in ticks}
    if extra_kinds or extra_ticks:
        raise ValueError(
            "bodies for slots that are not production slots: "
            f"{sorted(k.value for k in extra_kinds)} {sorted(extra_ticks)}"
        )
    handlers = tuple(
        HandlerSpec(
            kind,
            pol.pool,
            budget_s=pol.budget_s,
            lease_s=pol.lease_s,
            max_attempts=pol.max_attempts,
            handle=b.handlers.get(kind),
        )
        for kind, pol in sorted(policy.kinds.items(), key=lambda kv: kv[0].value)
    )
    bound_ticks = tuple(
        TickSpec(
            t.name,
            interval_s=t.interval_s,
            timeout_s=t.timeout_s,
            lock_key=t.lock_key,
            jitter_s=t.jitter_s,
            run_on_startup=t.run_on_startup,
            body=b.ticks.get(t.name),
        )
        for t in ticks
    )
    return Slots(handlers, bound_ticks)


def assert_all_bound(slots: Slots, *, enabled: bool) -> None:
    """Refuse to start with an unbound slot when assess is enabled (fail closed)."""
    missing = slots.unbound()
    if not enabled or not missing:
        return
    owners = {f"job:{k.value}": o for k, o in HANDLER_OWNERS.items()}
    owners |= {f"tick:{n}": o for n, o in TICK_OWNERS.items()}
    detail = ", ".join(f"{m} ({owners.get(m, '?')})" for m in missing)
    raise SlotsNotBound(
        f"VEKTRA_ASSESS_ENABLED=true but {len(missing)} worker slot(s) have no handler: {detail}"
    )

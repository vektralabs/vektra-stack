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
TEACHER = "20261101-154950-assess-teacher-ops"

# job kind -> plan that binds its handler
HANDLER_OWNERS: dict[JobKind, str] = {
    JobKind.PREFLIGHT: SESSION,
    JobKind.RECONCILE_REPLY: SESSION,
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
    JobKind.TEACHER_GATE: TEACHER,
    JobKind.SMTP_TEST_SEND: PRIVACY,
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
    "parser_ping": PRIVACY,  # plan 12b: the parser status /health/assess reports
    "setup_check": MOODLE_WS,
}

# --- the documented allowlist of unbound slots ---------------------------------------------------
# A production worker refuses to start while ANY slot is unbound, so every slot the fully keyed
# composition leaves unbound must be accounted for here, and nothing else may be unbound. The guard
# test (vektra-app/tests/test_assess_processes.py) composes the real worker and compares: a
# carried-over binding that silently vanished (setup_check, until 2026-11-08) fails it, and so does
# a slot bound without removing it from here.

#: plans not yet completed in .s2s/plans/INDEX-ASSESS.md: their slots are unbound by schedule (the
#: guard test checks each is still not ``completed`` there)
#: (plan 12a binds every privacy slot: purge tick and job, erasure job; the hardening plan's only
#: slot, ``rotate_keys``, was dropped in 0044: key rotation is the operator CLI ``keys rewrap``)
#: empty since 2026-11-10: a fully keyed production worker binds every slot
PENDING_OWNER_PLANS: frozenset[str] = frozenset()

#: slots whose owner plan IS completed but whose behaviour is not specified anywhere: a decision is
#: needed (recorded as an open item in the owner plan's Notes), not an invented body
#: none since 2026-11-09 (teacher_notify / operator_notify bound by the relay)
OPEN_DECISIONS: dict[str, str] = {}


def expected_unbound() -> set[str]:
    """The slots a fully keyed production composition is allowed to leave unbound today."""
    owned = {
        f"job:{k.value}" for k, o in HANDLER_OWNERS.items() if o in PENDING_OWNER_PLANS
    }
    owned |= {f"tick:{n}" for n, o in TICK_OWNERS.items() if o in PENDING_OWNER_PLANS}
    return owned | set(OPEN_DECISIONS)


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


#: QUIT's own bound after a session (``delivery.smtp.QUIT_TIMEOUT_S``) and room for the row write
PROBE_QUIT_S = 5.0
PROBE_ROW_MARGIN_S = 10.0
#: the smtp_probe tick's interval; the setup-check item ``smtp_probe`` judges freshness from it
SMTP_PROBE_INTERVAL_S = 300.0
#: the parser_ping tick's interval; /health/assess calls a parser status older than 3x stale
PARSER_PING_INTERVAL_S = 60.0


def probe_tick_timeout_s(smtp_timeout_s: float) -> float:
    """The smtp_probe tick's timeout: slot wait (= the SMTP timeout) + session + QUIT + row."""
    return 2 * smtp_timeout_s + PROBE_QUIT_S + PROBE_ROW_MARGIN_S


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
        # INT-003 readiness of the relay for /health/assess; never sends (design default). The
        # timeout covers a whole probe (bounded slot wait + session + QUIT) plus the row write
        t(
            "smtp_probe",
            SMTP_PROBE_INTERVAL_S,
            probe_tick_timeout_s(s.smtp_timeout_s),
            locks.SMTP_PROBE,
            jitter_s=30,
            run_on_startup=True,
        ),
        t("purge", 86_400, 600, locks.PURGE, jitter_s=3600),  # daily
        # plan 12b: the parser sidecar's status, published in the heartbeat row for /health/assess
        t(
            "parser_ping",
            PARSER_PING_INTERVAL_S,
            15,
            locks.PARSER_PING,
            jitter_s=10,
            run_on_startup=True,
        ),
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

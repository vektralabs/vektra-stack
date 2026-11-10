"""Production slot table (plan assess-job-loop B7): the exact set, owners, fail-closed binding."""

from __future__ import annotations

import pytest

from vektra_assess.config import VektraAssessSettings
from vektra_assess.jobs import JobKind
from vektra_assess.worker import locks
from vektra_assess.worker.policy import load_policy
from vektra_assess.worker.runner import JobContext
from vektra_assess.worker.slots import (
    HANDLER_OWNERS,
    TICK_OWNERS,
    Bindings,
    Slots,
    SlotsNotBound,
    assert_all_bound,
    build_slots,
)

# The exact production slot set. Adding a kind or a tick to the worker means changing this list in
# the same commit: an unreviewed (or test-only) slot in the production composition fails here.
PRODUCTION_KINDS = {
    "enrolment_recheck", "erasure", "gate_refresh", "preflight", "purge",
    "reconcile_reply", "render_check",
    "rotate_keys", "setup_check", "teacher_notify", "operator_notify", "transcript_deliver",
    "transcript_generate", "teacher_gate",
}  # fmt: skip
PRODUCTION_TICKS = [
    "heartbeat", "job_cleanup", "lease_reaper", "sweeper", "starting_reaper", "outage_probe",
    "enrolment_recheck", "gate_refresh", "delivery_retry", "smtp_probe", "purge", "parser_ping", "setup_check",
]  # fmt: skip


def _slots(bindings: Bindings | None = None) -> Slots:
    return build_slots(load_policy({}), VektraAssessSettings(), bindings)


def test_the_production_slot_set_is_exactly_the_reviewed_one() -> None:
    slots = _slots()
    assert {h.kind.value for h in slots.handlers} == PRODUCTION_KINDS
    assert {k.value for k in JobKind} == PRODUCTION_KINDS, (
        "a table kind without a slot never runs"
    )
    assert [t.name for t in slots.ticks] == PRODUCTION_TICKS
    assert set(HANDLER_OWNERS) == set(JobKind)
    assert set(TICK_OWNERS) == set(PRODUCTION_TICKS)


def test_every_tick_has_its_own_lock_from_the_registry_and_a_sane_schedule() -> None:
    slots = _slots()
    keys = [t.lock_key for t in slots.ticks]
    assert len(set(keys)) == len(keys)
    assert set(keys) <= set(locks.all_keys().values())
    for t in slots.ticks:
        assert 0 <= t.jitter_s < t.interval_s and t.timeout_s > 0
    by = {t.name: t for t in slots.ticks}
    assert by["heartbeat"].interval_s == 5.0
    assert by["job_cleanup"].interval_s == 3600
    assert by["setup_check"].run_on_startup and by["setup_check"].interval_s == 86_400
    # the SMTP probe (plan 10b) reports the relay's readiness as soon as the worker is up
    # ... and so does the parser ping (plan 12b), for /health/assess
    assert [t.name for t in slots.ticks if t.run_on_startup] == [
        "smtp_probe",
        "parser_ping",
        "setup_check",
    ]


def test_every_handler_takes_budget_lease_and_attempts_from_the_policy() -> None:
    policy = load_policy({"VEKTRA_ASSESS_JOB_PURGE_BUDGET_S": "90"})
    slots = build_slots(policy, VektraAssessSettings())
    purge = next(h for h in slots.handlers if h.kind is JobKind.PURGE)
    assert (purge.pool, purge.budget_s, purge.lease_s) == ("maintenance", 90, 180)
    assert all(h.lease_s >= 2 * h.budget_s for h in slots.handlers)


def test_unbound_slots_refuse_start_only_when_enabled_and_name_their_owner() -> None:
    slots = _slots()
    assert len(slots.unbound()) == len(PRODUCTION_KINDS) + len(PRODUCTION_TICKS)
    assert_all_bound(slots, enabled=False)  # dormant by default: no refusal
    with pytest.raises(SlotsNotBound) as exc:
        assert_all_bound(slots, enabled=True)
    msg = str(exc.value)
    assert "job:transcript_generate (20261101-154950-assess-transcript-delivery)" in msg
    assert "tick:sweeper (20261101-154950-assess-session-lifecycle)" in msg


async def test_fully_bound_slots_start() -> None:
    async def handle(ctx: JobContext) -> None: ...

    async def body() -> None: ...

    slots = _slots(
        Bindings({k: handle for k in JobKind}, {n: body for n in PRODUCTION_TICKS})
    )
    assert slots.unbound() == []
    assert_all_bound(slots, enabled=True)


def test_a_body_for_a_slot_outside_the_table_is_rejected() -> None:
    async def body() -> None: ...

    with pytest.raises(ValueError, match="not production slots"):
        _slots(Bindings({}, {"chaos_effect": body}))


@pytest.mark.parametrize("timeout_s", [5, 30, 60])
def test_the_smtp_probe_tick_fits_a_whole_probe(timeout_s: int) -> None:
    """Plan 10b review #7: bounded slot wait + session timeout + QUIT must fit the tick."""
    from vektra_assess.delivery.smtp import QUIT_TIMEOUT_S

    settings = VektraAssessSettings.model_validate(
        {"VEKTRA_ASSESS_SMTP_TIMEOUT_S": timeout_s}
    )
    tick = next(
        t
        for t in build_slots(load_policy({}), settings).ticks
        if t.name == "smtp_probe"
    )
    assert tick.timeout_s > 2 * timeout_s + QUIT_TIMEOUT_S
    assert tick.timeout_s < tick.interval_s


def test_the_health_check_judges_the_parser_on_the_ping_interval() -> None:
    import inspect

    from vektra_assess.health import make_readiness_check
    from vektra_assess.worker.slots import PARSER_PING_INTERVAL_S

    default = (
        inspect.signature(make_readiness_check).parameters["parser_interval_s"].default
    )
    assert default == PARSER_PING_INTERVAL_S

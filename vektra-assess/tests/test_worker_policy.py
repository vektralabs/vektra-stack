"""Job policy loading (plan assess-job-loop B4): defaults, overrides, fail-closed validation."""

from __future__ import annotations

import pytest

from vektra_assess.config import VektraAssessSettings
from vektra_assess.jobs import JobKind
from vektra_assess.worker.policy import (
    KIND_DEFAULTS,
    POOL_DEFAULTS,
    WorkerPolicyError,
    load_policy,
)


def test_every_job_kind_has_a_policy_in_a_declared_pool() -> None:
    assert set(KIND_DEFAULTS) == set(JobKind), "a kind without a policy would never run"
    for kind, p in KIND_DEFAULTS.items():
        assert p.pool in POOL_DEFAULTS, kind
        assert p.lease_s >= 2 * p.budget_s, kind


def test_defaults_match_the_plan_table() -> None:
    pol = load_policy({})
    assert {p.name: p.concurrency for p in pol.pools} == {
        "preflight": 10, "ws": 2, "transcript": 2, "delivery": 2, "setup": 1, "maintenance": 1,
    }  # fmt: skip
    ws = pol.kinds[JobKind.GATE_REFRESH]
    assert (ws.pool, ws.budget_s, ws.lease_s) == ("ws", 20, 40)
    setup = pol.kinds[JobKind.SETUP_CHECK]
    assert (setup.pool, setup.budget_s, setup.lease_s) == ("setup", 120, 240)


def test_overrides_for_pool_budget_lease_and_attempts() -> None:
    pol = load_policy(
        {
            "VEKTRA_ASSESS_POOL_WS_CONCURRENCY": "4",
            "VEKTRA_ASSESS_PREFLIGHT_CONCURRENCY": "25",
            "VEKTRA_ASSESS_JOB_PURGE_BUDGET_S": "90",
            "VEKTRA_ASSESS_JOB_PURGE_MAX_ATTEMPTS": "2",
            "VEKTRA_ASSESS_JOB_RENDER_CHECK_LEASE_S": "100",
        }
    )
    conc = {p.name: p.concurrency for p in pol.pools}
    assert conc["ws"] == 4 and conc["preflight"] == 25
    purge = pol.kinds[JobKind.PURGE]
    assert (purge.budget_s, purge.lease_s, purge.max_attempts) == (90, 180, 2)
    assert pol.kinds[JobKind.RENDER_CHECK].lease_s == 100


@pytest.mark.parametrize(
    ("env", "needle"),
    [
        ({"VEKTRA_ASSESS_JOB_PURGE_BUDGET_S": "60", "VEKTRA_ASSESS_JOB_PURGE_LEASE_S": "119"}, "at least 2 x budget_s"),
        ({"VEKTRA_ASSESS_JOB_PURGE_LEASE_S": "10"}, "at least 2 x budget_s"),
        ({"VEKTRA_ASSESS_JOB_PURGE_BUDGET_S": "fast"}, "not a number"),
        ({"VEKTRA_ASSESS_POOL_WS_CONCURRENCY": "0"}, "positive"),
        ({"VEKTRA_ASSESS_JOB_PURGE_BUDGET_S": "nan"}, "finite"),
        ({"VEKTRA_ASSESS_JOB_PURGE_LEASE_S": "inf"}, "finite"),
        ({"VEKTRA_ASSESS_JOB_PURGE_BUDGET_S": "-inf"}, "finite"),
        ({"VEKTRA_ASSESS_JOB_PURGE_MAX_ATTEMPTS": "1.5"}, "not a number"),
        ({"VEKTRA_ASSESS_JOB_PURGE_MAX_ATTEMPTS": "3000000000"}, "must not exceed"),
        ({"VEKTRA_ASSESS_POOL_MAINTENANCE_CONCURRENCY": "3000000000"}, "must not exceed"),
        ({"VEKTRA_ASSESS_JOB_PURGE_LEASE_S": "1e300"}, "must not exceed"),
        ({"VEKTRA_ASSESS_JOB_PURGE_BUDGET_S": "1e300"}, "must not exceed"),
        ({"VEKTRA_ASSESS_JOB_PURGE_BUDGET_S": "50000"}, "derived lease"),
        ({"VEKTRA_ASSESS_JOB_PURG_BUDGET_S": "10"}, "unknown job setting"),
        ({"VEKTRA_ASSESS_POOL_NOPE_CONCURRENCY": "3"}, "unknown job setting"),
        (
            {"VEKTRA_ASSESS_POOL_PREFLIGHT_CONCURRENCY": "3", "VEKTRA_ASSESS_PREFLIGHT_CONCURRENCY": "4"},
            "only one of",
        ),
    ],
)  # fmt: skip
def test_invalid_policy_is_refused_before_any_claim(
    env: dict[str, str], needle: str
) -> None:
    with pytest.raises(WorkerPolicyError, match=needle):
        load_policy(env)


def test_unrelated_environment_is_ignored() -> None:
    assert load_policy({"PATH": "/bin", "VEKTRA_ASSESS_ENABLED": "true"}).pools


def test_scalar_settings_defaults_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    s = VektraAssessSettings()
    assert (s.job_poll_interval_s, s.worker_drain_s, s.job_done_retention_h) == (
        1.0,
        20.0,
        24,
    )
    assert (
        s.worker_heartbeat_interval_s,
        s.job_backoff_base_s,
        s.job_backoff_cap_s,
    ) == (5.0, 5.0, 300.0)
    # three beats must fit in the readiness window, and the drain under the 30 s stop grace
    assert s.heartbeat_max_age_s >= 3 * s.worker_heartbeat_interval_s
    monkeypatch.setenv("VEKTRA_ASSESS_WORKER_DRAIN_S", "30")
    with pytest.raises(ValueError):
        VektraAssessSettings()


def test_the_typo_check_accepts_every_real_setting_that_shares_the_prefix() -> None:
    """A strict prefix scan must not reject the settings class's own ``VEKTRA_ASSESS_JOB_*``."""
    own = {
        f.alias: "1"
        for f in VektraAssessSettings.model_fields.values()
        if f.alias and f.alias.startswith(("VEKTRA_ASSESS_JOB_", "VEKTRA_ASSESS_POOL_"))
    }
    assert (
        "VEKTRA_ASSESS_JOB_POLL_INTERVAL_S" in own
        and "VEKTRA_ASSESS_JOB_DONE_RETENTION_H" in own
    )
    assert load_policy(own).pools


def test_preflight_budget_and_lease_follow_start_t() -> None:
    """Review fix 10: budget = T - margin, lease above budget + margin (and >= 2 x budget)."""
    from vektra_assess.worker.policy import (
        PREFLIGHT_BUDGET_MARGIN_S,
        PREFLIGHT_LEASE_MARGIN_S,
    )

    for t in (60, 120, 20):
        p = load_policy({"VEKTRA_ASSESS_START_T_S": str(t)}).kinds[JobKind.PREFLIGHT]
        assert p.budget_s == t - PREFLIGHT_BUDGET_MARGIN_S
        assert p.lease_s >= p.budget_s + PREFLIGHT_LEASE_MARGIN_S
        assert p.lease_s >= 2 * p.budget_s
    p = load_policy({"VEKTRA_ASSESS_START_T_S": "120"}).kinds[JobKind.PREFLIGHT]
    assert (p.budget_s, p.lease_s) == (105, 210)


@pytest.mark.parametrize(
    "env",
    [
        # a budget the commit could not fit inside T
        {
            "VEKTRA_ASSESS_START_T_S": "120",
            "VEKTRA_ASSESS_JOB_PREFLIGHT_BUDGET_S": "118",
        },
        {"VEKTRA_ASSESS_START_T_S": "60", "VEKTRA_ASSESS_JOB_PREFLIGHT_BUDGET_S": "60"},
        # a lease without its margin over the budget
        {
            "VEKTRA_ASSESS_START_T_S": "120",
            "VEKTRA_ASSESS_JOB_PREFLIGHT_BUDGET_S": "50",
            "VEKTRA_ASSESS_JOB_PREFLIGHT_LEASE_S": "70",
        },
    ],
)
def test_incoherent_preflight_settings_refuse_to_start(env: dict[str, str]) -> None:
    with pytest.raises(WorkerPolicyError):
        load_policy(env)


def test_the_teacher_gate_budget_covers_its_two_moodle_stages() -> None:
    """CodeRabbit 4505511249: a refresh resolves the cmid (one call) and then reads the editors
    and the facts concurrently (one more call's deadline): the budget must cover two full WS
    deadlines plus a DB margin, from the configured deadline."""
    from vektra_assess.jobs import JobKind

    default = load_policy({}).kinds[JobKind.TEACHER_GATE]
    total = float(VektraAssessSettings.model_fields["ws_total_timeout_s"].default)
    assert default.budget_s >= 2 * total + 5 and default.lease_s >= 2 * default.budget_s
    slow = load_policy({"VEKTRA_ASSESS_WS_TOTAL_TIMEOUT_S": "90"}).kinds[
        JobKind.TEACHER_GATE
    ]
    assert slow.budget_s >= 2 * 90 + 5 and slow.lease_s >= 2 * slow.budget_s
    explicit = load_policy({"VEKTRA_ASSESS_JOB_TEACHER_GATE_BUDGET_S": "100"})
    assert explicit.kinds[JobKind.TEACHER_GATE].budget_s == 100

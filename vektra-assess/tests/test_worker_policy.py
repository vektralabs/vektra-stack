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
        ({"VEKTRA_ASSESS_JOB_PURGE_MAX_ATTEMPTS": "1.5"}, "not a number"),
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

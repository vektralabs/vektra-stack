"""Per-pool and per-kind job policy: defaults plus validated environment overrides.

Concurrency belongs to a **pool** (several kinds share one ``asyncio`` limit, so a stalled Moodle
call cannot starve pre-flight); budget, lease and attempts belong to a **kind**. Overrides:

- ``VEKTRA_ASSESS_POOL_<POOL>_CONCURRENCY`` (the pre-flight pool also answers to the older
  ``VEKTRA_ASSESS_PREFLIGHT_CONCURRENCY``, the NFR-020 admission number K);
- ``VEKTRA_ASSESS_JOB_<KIND>_BUDGET_S`` / ``_LEASE_S`` / ``_MAX_ATTEMPTS``.

Loading is strict and fails closed: a lease shorter than twice the budget (no renewal exists, so
a second worker would reclaim a job still inside its own timeout), a non-number, or a name with
the right prefix that matches no pool or kind (a typo that would silently leave the default in
force) all raise ``WorkerPolicyError`` before the worker claims anything.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from vektra_assess.config import VektraAssessSettings
from vektra_assess.jobs import JobKind
from vektra_assess.worker.runtime import PoolSpec


class WorkerPolicyError(ValueError):
    """The job policy is invalid; the worker refuses to start."""


@dataclass(frozen=True, slots=True)
class KindPolicy:
    pool: str
    budget_s: float
    lease_s: float
    max_attempts: int = 5


# pool -> default concurrency. The ``preflight`` pool runs the ``preflight`` kind (plan
# session-lifecycle); K is configured in one place (NFR-020, placeholder 10 until OQ-026 sets the
# hardware).
POOL_DEFAULTS: dict[str, int] = {
    "preflight": 10,
    "ws": 2,
    "transcript": 2,
    "delivery": 2,
    "setup": 1,
    "maintenance": 1,
}

_WS = KindPolicy("ws", 20, 40)
_MAINT = KindPolicy("maintenance", 60, 120)

#: pre-flight timing is derived from the REQ-071 commit window T (``VEKTRA_ASSESS_START_T_S``):
#: budget = T - PREFLIGHT_BUDGET_MARGIN_S, so the runtime cancels a stuck pre-flight while there is
#: still time to roll it back inside T; lease >= max(2 x budget, budget + PREFLIGHT_LEASE_MARGIN_S),
#: so no second worker reclaims a pre-flight still inside its budget, and the STARTING reaper
#: (which waits for T AND no live lease) never races a live handler. The handler itself keeps
#: COMMIT_MARGIN_S of T for its commit transaction: a budget past T - COMMIT_MARGIN_S is refused.
PREFLIGHT_BUDGET_MARGIN_S = 15.0
PREFLIGHT_LEASE_MARGIN_S = 30.0
COMMIT_MARGIN_S = 5.0


def preflight_defaults(start_t_s: float) -> KindPolicy:
    budget = max(start_t_s - PREFLIGHT_BUDGET_MARGIN_S, 1.0)
    return KindPolicy(
        "preflight", budget, max(2 * budget, budget + PREFLIGHT_LEASE_MARGIN_S), 2
    )


KIND_DEFAULTS: dict[JobKind, KindPolicy] = {
    # the default T (60 s); ``load_policy`` re-derives it from the configured T
    JobKind.PREFLIGHT: preflight_defaults(60),
    JobKind.ENROLMENT_RECHECK: _WS,
    JobKind.GATE_REFRESH: _WS,
    JobKind.RENDER_CHECK: _WS,
    JobKind.TEACHER_GATE: _WS,  # plan 11: a handful of Moodle reads per activity
    JobKind.CAPABILITY_PROBE: _WS,
    JobKind.TRANSCRIPT_GENERATE: KindPolicy("transcript", 30, 60),
    # up to 3 recipients through at most VEKTRA_ASSESS_SMTP_MAX_CONNECTIONS sessions, each with
    # a 30 s hard timeout (plan transcript-delivery 10b); the lease is also the crash window
    # after which an attempt with no outcome is taken again (OQ-029)
    JobKind.TRANSCRIPT_DELIVER: KindPolicy("delivery", 120, 240),
    JobKind.TEACHER_NOTIFY: KindPolicy("delivery", 30, 60),
    JobKind.OPERATOR_NOTIFY: KindPolicy("delivery", 30, 60),
    # the setup check makes about twenty live Moodle calls: own pool, 120 s hard timeout
    JobKind.SETUP_CHECK: KindPolicy("setup", 120, 240),
    JobKind.PURGE: _MAINT,
    JobKind.ROTATE_KEYS: _MAINT,
    JobKind.ERASURE: _MAINT,
    JobKind.SESSION_DEADLINE: _MAINT,
    # unwrap the DEK, append one marked turn: short, its own budget in the maintenance pool
    JobKind.RECONCILE_REPLY: KindPolicy("maintenance", 20, 40, 3),
}

_POOL_PREFIX = "VEKTRA_ASSESS_POOL_"
_JOB_PREFIX = "VEKTRA_ASSESS_JOB_"
_PREFLIGHT_ALIAS = "VEKTRA_ASSESS_PREFLIGHT_CONCURRENCY"
_KIND_FIELDS = ("BUDGET_S", "LEASE_S", "MAX_ATTEMPTS")


@dataclass(frozen=True, slots=True)
class WorkerPolicy:
    pools: tuple[PoolSpec, ...]
    kinds: Mapping[JobKind, KindPolicy]


# Upper bounds: a value past the column type (int4, interval) passes a bare "positive and finite"
# check and then fails inside every claim, leaving a worker that looks ready and never claims.
MAX_COUNT = 10_000
MAX_SECONDS = 86_400.0


def _number(
    name: str,
    raw: str,
    kind: type[int] | type[float],
    maximum: float | None = None,
) -> float:
    try:
        value = kind(raw)
    except ValueError:
        raise WorkerPolicyError(f"{name}={raw!r} is not a number") from None
    if not math.isfinite(value) or value <= 0:
        raise WorkerPolicyError(f"{name} must be a positive, finite number")
    if maximum is not None and value > maximum:
        raise WorkerPolicyError(f"{name} must not exceed {maximum:g}")
    return float(value)


def _start_t(env: Mapping[str, str]) -> float:
    """``VEKTRA_ASSESS_START_T_S`` validated by the settings class itself (its range included)."""
    from pydantic import ValidationError

    raw = env.get("VEKTRA_ASSESS_START_T_S")
    if raw is None:
        return float(VektraAssessSettings.model_fields["start_t_s"].default)
    try:
        return float(
            VektraAssessSettings.model_validate(
                {"VEKTRA_ASSESS_START_T_S": raw}
            ).start_t_s
        )
    except ValidationError:
        raise WorkerPolicyError(
            f"VEKTRA_ASSESS_START_T_S={raw!r} is not a valid T"
        ) from None


def load_policy(env: Mapping[str, str]) -> WorkerPolicy:
    pools = dict(POOL_DEFAULTS)
    kinds = dict(KIND_DEFAULTS)
    # The typo check must not reject the other settings that share the prefix
    # (VEKTRA_ASSESS_JOB_POLL_INTERVAL_S, ..._BACKOFF_BASE_S): they are the settings class's own.
    known: set[str] = {_PREFLIGHT_ALIAS} | {
        f.alias for f in VektraAssessSettings.model_fields.values() if f.alias
    }

    for pool in POOL_DEFAULTS:
        name = f"{_POOL_PREFIX}{pool.upper()}_CONCURRENCY"
        known.add(name)
        raw = env.get(name)
        if pool == "preflight" and _PREFLIGHT_ALIAS in env:
            if raw is not None:
                raise WorkerPolicyError(
                    f"set only one of {name} and {_PREFLIGHT_ALIAS}"
                )
            name, raw = _PREFLIGHT_ALIAS, env[_PREFLIGHT_ALIAS]
        if raw is not None:
            pools[pool] = int(_number(name, raw, int, MAX_COUNT))

    start_t = _start_t(env)
    for kind, base in KIND_DEFAULTS.items():
        if kind is JobKind.PREFLIGHT:
            base = preflight_defaults(start_t)
        names = {f: f"{_JOB_PREFIX}{kind.value.upper()}_{f}" for f in _KIND_FIELDS}
        known.update(names.values())
        budget, lease, attempts = base.budget_s, base.lease_s, base.max_attempts
        explicit_budget = names["BUDGET_S"] in env
        if explicit_budget:
            budget = _number(
                names["BUDGET_S"], env[names["BUDGET_S"]], float, MAX_SECONDS
            )
        if names["LEASE_S"] in env:
            lease = _number(names["LEASE_S"], env[names["LEASE_S"]], float, MAX_SECONDS)
        elif explicit_budget:
            lease = 2 * budget  # keeps the 2 x rule without forcing two overrides
            if lease > MAX_SECONDS:
                raise WorkerPolicyError(
                    f"{names['BUDGET_S']} must not exceed {MAX_SECONDS / 2:g} "
                    f"(the derived lease would pass {MAX_SECONDS:g})"
                )
        if names["MAX_ATTEMPTS"] in env:
            attempts = int(
                _number(
                    names["MAX_ATTEMPTS"],
                    env[names["MAX_ATTEMPTS"]],
                    int,
                    MAX_COUNT,
                )
            )
        if lease < 2 * budget:
            raise WorkerPolicyError(
                f"{kind.value}: lease_s ({lease:g}) must be at least 2 x budget_s ({budget:g})"
            )
        if kind is JobKind.PREFLIGHT:
            if budget > start_t - COMMIT_MARGIN_S:
                raise WorkerPolicyError(
                    f"preflight: budget_s ({budget:g}) must leave {COMMIT_MARGIN_S:g} s of "
                    f"VEKTRA_ASSESS_START_T_S ({start_t:g}) for the commit or the rollback"
                )
            if lease < budget + PREFLIGHT_LEASE_MARGIN_S:
                raise WorkerPolicyError(
                    f"preflight: lease_s ({lease:g}) must be at least budget_s + "
                    f"{PREFLIGHT_LEASE_MARGIN_S:g} ({budget + PREFLIGHT_LEASE_MARGIN_S:g})"
                )
        kinds[kind] = KindPolicy(base.pool, budget, lease, attempts)

    for key in env:
        if key.startswith((_POOL_PREFIX, _JOB_PREFIX)) and key not in known:
            raise WorkerPolicyError(f"unknown job setting {key}: no such pool or kind")

    return WorkerPolicy(
        pools=tuple(PoolSpec(name, n) for name, n in pools.items()), kinds=kinds
    )

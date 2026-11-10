"""Assess readiness check for /health/assess (plan assess-foundations 8.2, data-crypto 8.1).

Registered as ``("health", "assess")`` by the API's startup step 12 whether assess is
enabled or not, so ``GET /health/assess`` always answers. It is excluded from the shallow
``/health`` (``vektra_admin.health.SHALLOW_EXCLUDED``): the worker is another container.

When enabled it never reports ``healthy`` without evidence. The evidence is the worker's
row in ``assess_heartbeat``, read over the API role (which has SELECT on it) and aged by
the database clock, so container clock skew cannot make a dead worker look alive:

- no engine wired            -> degraded, ``heartbeat_source_missing``
- no row                     -> degraded, ``no_heartbeat``
- older than ``max_age_s``   -> degraded, ``heartbeat_stale``
- worker state not ``ready`` -> degraded, ``worker_not_ready`` (starting, draining or stopped)
- oldest ready job older than ``BACKLOG_MAX_AGE_S`` -> degraded, ``job_backlog`` (the worker
  publishes that figure in the row because the API role cannot read ``assess_jobs``)
- database unreachable       -> unhealthy, ``heartbeat_unreadable``
- the API's own keys missing -> degraded, ``keys_missing`` (plan 12b)
- otherwise                  -> healthy

Plan assess-privacy-retention 12b adds content-free ``details`` to the status (ages, flags,
counts; no id, no site name, no error message): the worker heartbeat age and state, the oldest
ready job, the parser status the worker's ``parser_ping`` tick publishes, the SMTP probe (its
``last`` verdict while a probe is busy), how many active sites have complete privacy settings,
whether the API holds its keys, and the age of the last successful purge. ``details`` never
changes the verdict above except ``keys_missing``: SMTP, parser and purge have their own alerts
(``deploy/prometheus/assess-alerts.yml.example``). The check is marked ``readiness = True``, so
``GET /health/assess`` answers 503 unless the status is ``healthy`` or ``disabled``.

Owning plan: .s2s/plans/20261101-154950-assess-data-crypto.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from sqlalchemy import text

from vektra_shared.types import HealthStatus

DISABLED = "disabled"
HEARTBEAT_SOURCE_MISSING = "heartbeat_source_missing"
NO_HEARTBEAT = "no_heartbeat"
HEARTBEAT_STALE = "heartbeat_stale"
HEARTBEAT_UNREADABLE = "heartbeat_unreadable"
WORKER_NOT_READY = "worker_not_ready"
JOB_BACKLOG = "job_backlog"
KEYS_MISSING = "keys_missing"
BACKLOG_MAX_AGE_S = 30.0
WORKER_COMPONENT = "worker"

#: a parser status older than this many ping intervals is ``stale``
PARSER_STALE_FACTOR = 3

_DETAILS_SQL = text(
    "SELECT extract(epoch FROM now()), "
    "(SELECT details FROM assess_heartbeat WHERE component = 'worker'), "
    "(SELECT extract(epoch FROM now() - beat_at) FROM assess_heartbeat WHERE component = 'smtp'), "
    "(SELECT details FROM assess_heartbeat WHERE component = 'smtp'), "
    "(SELECT details FROM assess_heartbeat WHERE component = 'purge'), "
    "(SELECT count(*) FROM assess_sites WHERE disabled_at IS NULL)"
)
_PRIVACY_SQL = text(
    "SELECT p.site_id, p.retention_days, p.legal_basis_label, p.legal_basis_text, "
    "p.notice_url, p.notice_text, p.notice_version FROM assess_privacy_config p "
    "JOIN assess_sites s ON s.id = p.site_id WHERE s.disabled_at IS NULL"
)

_AGE_SQL = text(
    "SELECT extract(epoch FROM now() - beat_at), details->>'state', "
    "details->>'oldest_ready_age_s' FROM assess_heartbeat WHERE component = :c"
)


def _num(v: object) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return round(float(v), 1)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def readiness_details(
    *,
    worker_age_s: float | None,
    row: tuple[Any, ...] | None,
    privacy_rows: Iterable[Any],
    keys_present: bool,
    parser_interval_s: float,
) -> dict[str, Any]:
    """The content-free figures of ``/health/assess`` (plan 12b). Pure: easy to pin."""
    from vektra_assess.privacy.config import passes

    now, worker, smtp_age, smtp, purge, sites = row if row is not None else (None,) * 6
    worker = worker if isinstance(worker, Mapping) else {}
    smtp = smtp if isinstance(smtp, Mapping) else {}
    purge = purge if isinstance(purge, Mapping) else {}
    now_f = _num(now)

    parser: dict[str, Any] = {"status": "unknown", "age_s": None}
    pinfo = worker.get("parser")
    if isinstance(pinfo, Mapping) and isinstance(pinfo.get("ok"), bool):
        at = _num(pinfo.get("at"))
        age = round(now_f - at, 1) if now_f is not None and at is not None else None
        if age is not None and age > PARSER_STALE_FACTOR * parser_interval_s:
            parser = {"status": "stale", "age_s": age}
        else:
            parser = {"status": "ok" if pinfo["ok"] else "down", "age_s": age}

    verdict = smtp.get("last") if smtp.get("state") == "busy" else smtp
    smtp_ok = verdict.get("ok") if isinstance(verdict, Mapping) else None
    smtp_status = (
        "unknown" if not isinstance(smtp_ok, bool) else ("ok" if smtp_ok else "failed")
    )

    last_success = _num(purge.get("last_success"))
    purge_age = (
        round(now_f - last_success, 1)
        if now_f is not None and last_success is not None
        else None
    )
    last_ok = purge.get("ok")

    configured = sum(1 for r in privacy_rows if passes(r))
    return {
        "worker_heartbeat_age_s": _num(worker_age_s),
        "worker_state": worker.get("state")
        if isinstance(worker.get("state"), str)
        else None,
        "oldest_ready_job_age_s": _num(worker.get("oldest_ready_age_s")),
        "parser": parser,
        "smtp_probe": {"status": smtp_status, "age_s": _num(smtp_age)},
        "privacy": {"active_sites": int(sites or 0), "configured_sites": configured},
        "keys_present": bool(keys_present),
        "purge": {
            "last_success_age_s": purge_age,
            "last_run_ok": last_ok if isinstance(last_ok, bool) else None,
        },
    }


def make_readiness_check(
    *,
    enabled: bool,
    get_engine: Callable[[], Any | None] | None = None,
    max_age_s: int = 60,
    keys_present: Callable[[], bool] | None = None,
    parser_interval_s: float = 60.0,
) -> Callable[[], Awaitable[HealthStatus]]:
    async def assess_readiness() -> HealthStatus:
        status, worker_age = await _verdict()
        engine = get_engine() if get_engine is not None else None
        if status.status == DISABLED or engine is None:
            return status
        have_keys = keys_present() if keys_present is not None else True
        if not have_keys and status.status == "healthy":
            status = HealthStatus(status="degraded", message=KEYS_MISSING)
        if status.message == HEARTBEAT_UNREADABLE:
            return HealthStatus(
                status=status.status,
                message=status.message,
                details={"keys_present": have_keys, "figures": "unreadable"},
            )
        try:
            async with engine.connect() as conn:
                row = (await conn.execute(_DETAILS_SQL)).first()
                privacy = (await conn.execute(_PRIVACY_SQL)).all()
            details = readiness_details(
                worker_age_s=worker_age,
                row=tuple(row) if row is not None and len(row) == 6 else None,
                privacy_rows=privacy,
                keys_present=have_keys,
                parser_interval_s=parser_interval_s,
            )
        except Exception:
            details = {"keys_present": have_keys, "figures": "unreadable"}
        return HealthStatus(
            status=status.status, message=status.message, details=details
        )

    async def _verdict() -> tuple[HealthStatus, float | None]:
        if not enabled:
            return HealthStatus(
                status=DISABLED, message="VEKTRA_ASSESS_ENABLED=false"
            ), None
        engine = get_engine() if get_engine is not None else None
        if engine is None:
            return HealthStatus(
                status="degraded", message=HEARTBEAT_SOURCE_MISSING
            ), None
        try:
            async with engine.connect() as conn:
                row = (await conn.execute(_AGE_SQL, {"c": WORKER_COMPONENT})).first()
        except Exception:
            return HealthStatus(status="unhealthy", message=HEARTBEAT_UNREADABLE), None
        if row is None or row[0] is None:
            return HealthStatus(status="degraded", message=NO_HEARTBEAT), None
        age, state, backlog = row
        age = float(age)
        if age > max_age_s:
            return HealthStatus(status="degraded", message=HEARTBEAT_STALE), age
        # Optional fields: a row without them (an older worker) is judged on its age alone.
        if state is not None and state != "ready":
            return HealthStatus(status="degraded", message=WORKER_NOT_READY), age
        if backlog is not None and float(backlog) > BACKLOG_MAX_AGE_S:
            return HealthStatus(status="degraded", message=JOB_BACKLOG), age
        return HealthStatus(status="healthy"), age

    assess_readiness.readiness = True  # type: ignore[attr-defined]
    return assess_readiness

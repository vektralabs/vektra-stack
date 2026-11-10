"""Content-free metrics snapshot for the API's /metrics (plan assess-privacy-retention, 12b).

- ``assess_metrics_snapshot()``: SECURITY DEFINER, owned by ``vektra_assess_definer``, EXECUTE for
  the API only. It returns ONE jsonb object of counts and ages and nothing else: no id, no site,
  no user, no course, no text. Grouping keys are the CHECK-constrained enums of the columns they
  come from (session state, close reason, delivery status, Start rollback reason), and the
  collector maps any value outside its fixed enum to ``other`` before it becomes a label.
- The definer may now SELECT the rows it counts: ``assess_jobs`` (oldest ready job),
  ``assess_start_attempts`` (rollbacks and
  capacity rollbacks) and ``assess_heartbeat`` (worker, smtp probe and purge rows). The API role
  gains nothing but the EXECUTE: it still cannot read jobs, audit events or deliveries. The audit table stays
  insert-only for every role, so the API's ADVISORY capacity refusals (audit rows only) are not
  counted: the series counts the pre-flight's capacity rollbacks.

The operator's SMTP test rows (``kind = 'smtp_test'``, revision 0042) are left out of the
delivery figures: a test send must never fire a production delivery alert.

Revision ID: 0041
Revises: 0040
"""

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

DEFINER = "vektra_assess_definer"
SIG = "public.assess_metrics_snapshot()"

#: (the statement it follows, the new statements), on revision 0040's frozen matrix
_INSERT_AFTER = (
    (
        "GRANT SELECT ON assess_deliveries TO vektra_assess_definer",
        ("GRANT SELECT ON assess_heartbeat TO vektra_assess_definer",),
    ),
    (
        "GRANT INSERT ON assess_jobs TO vektra_assess_definer",
        ("GRANT SELECT ON assess_jobs TO vektra_assess_definer",),
    ),
    (
        "GRANT INSERT ON assess_start_attempts TO vektra_assess_definer",
        ("GRANT SELECT ON assess_start_attempts TO vektra_assess_definer",),
    ),
    (
        "GRANT EXECUTE ON FUNCTION public.assess_enter_outage(uuid,integer,timestamp with "
        "time zone,text) TO vektra_assess_api",
        (
            "REVOKE ALL ON FUNCTION public.assess_metrics_snapshot() FROM PUBLIC, "
            "vektra_assess_api, vektra_assess_worker",
            "GRANT EXECUTE ON FUNCTION public.assess_metrics_snapshot() TO vektra_assess_api",
        ),
    ),
)


def _rev(name: str):  # type: ignore[no-untyped-def]
    from importlib import util
    from pathlib import Path

    spec = util.spec_from_file_location(
        f"rev_{name[:4]}", Path(__file__).with_name(name)
    )
    assert spec is not None and spec.loader is not None
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _statements() -> tuple[str, ...]:
    out: list[str] = []
    after = dict(_INSERT_AFTER)
    for stmt in _rev("0040_assess_privacy_retention.py").GRANT_STATEMENTS:
        out.append(stmt)
        out.extend(after.get(stmt, ()))
    return tuple(out)


GRANT_STATEMENTS = (*_statements(),)

SNAPSHOT_FN = """
    CREATE FUNCTION public.assess_metrics_snapshot() RETURNS jsonb
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        SELECT jsonb_build_object(
            'sessions', COALESCE((SELECT jsonb_object_agg(state, n) FROM (
                SELECT state, count(*) AS n FROM public.assess_sessions GROUP BY state) s),
                '{}'::jsonb),
            'closed', COALESCE((SELECT jsonb_object_agg(close_reason, n) FROM (
                SELECT close_reason, count(*) AS n FROM public.assess_sessions
                WHERE state = 'CLOSED' GROUP BY close_reason) c), '{}'::jsonb),
            'deliveries', COALESCE((SELECT jsonb_object_agg(status, n) FROM (
                SELECT status, count(*) AS n FROM public.assess_deliveries
                WHERE kind <> 'smtp_test' GROUP BY status) d), '{}'::jsonb),
            'retrying_oldest_s', (SELECT extract(epoch FROM now() - min(created_at))
                FROM public.assess_deliveries WHERE status = 'RETRYING'
                AND kind <> 'smtp_test'),
            'tls_failures', (SELECT count(*) FROM public.assess_deliveries
                WHERE last_error_class = 'tls' AND kind <> 'smtp_test'
                AND status IN ('PENDING', 'RETRYING', 'FAILED')),
            'capacity_refusals', (SELECT count(*) FROM public.assess_start_attempts
                WHERE outcome IN ('rolled_back', 'refused') AND reason_code = 'capacity'),
            'rollbacks', COALESCE((SELECT jsonb_object_agg(reason, n) FROM (
                SELECT COALESCE(reason_code, 'none') AS reason, count(*) AS n
                FROM public.assess_start_attempts WHERE outcome = 'rolled_back'
                GROUP BY 1) r), '{}'::jsonb),
            'jobs_oldest_ready_s', (SELECT extract(epoch FROM now() - min(not_before))
                FROM public.assess_jobs WHERE state = 'ready' AND not_before <= now()),
            'worker_heartbeat_age_s', (SELECT extract(epoch FROM now() - beat_at)
                FROM public.assess_heartbeat WHERE component = 'worker'),
            'smtp_probe_ok', (SELECT CASE
                    WHEN details->>'state' = 'busy' THEN (details->'last'->>'ok')::boolean
                    ELSE (details->>'ok')::boolean END
                FROM public.assess_heartbeat WHERE component = 'smtp'),
            'smtp_probe_age_s', (SELECT extract(epoch FROM now() - beat_at)
                FROM public.assess_heartbeat WHERE component = 'smtp'),
            'purge_last_success', (SELECT (details->>'last_success')::double precision
                FROM public.assess_heartbeat WHERE component = 'purge'),
            'purge_rows_deleted', (SELECT (details->>'rows_deleted')::bigint
                FROM public.assess_heartbeat WHERE component = 'purge'),
            'purge_last_ok', (SELECT (details->>'ok')::boolean
                FROM public.assess_heartbeat WHERE component = 'purge')
        )
    $$
"""


def upgrade() -> None:
    op.execute(SNAPSHOT_FN)
    # ALTER FUNCTION ... OWNER TO needs CREATE on the schema (revoked again by the matrix below)
    op.execute(f"GRANT CREATE ON SCHEMA public TO {DEFINER}")
    op.execute(f"ALTER FUNCTION {SIG} OWNER TO {DEFINER}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SIG} FROM PUBLIC")
    for stmt in GRANT_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIG}")
    for stmt in _rev("0040_assess_privacy_retention.py").GRANT_STATEMENTS:
        op.execute(stmt)

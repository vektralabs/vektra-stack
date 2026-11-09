"""Privacy settings, purge tombstones and erasure requests (plan assess-privacy-retention, 12a).

- ``assess_privacy_config``: one row per site, the REQ-089 settings the operator writes (values
  supplied by the DPO, OQ-020). A site without a row, or with any field out of bounds, is not
  configured: the teacher's enable and the Start are refused (fail closed). The CHECKs repeat the
  application's bounds, so no path can store a row the application would call configured
  when it is not.
- ``assess_purge_records``: the tombstones. Identifiers and counts only (the site, a reference of
  the subject ``sha256(site|userid)``, the session, its transcript id, session number, activity,
  why and when, and the per-table deleted counts). One per session: a second purge of the same
  session (an erasure after a retention purge) keeps the first. They answer 410 for a purged
  transcript and ``transcripts[].purged`` in the state, and are the erasure records of ARCH-077.
- ``assess_erasure_records`` (revision 0012, never written before) becomes the erasure request
  the API queues and the worker completes: ``scope`` (one subject, or one activity with an
  optional user list), ``cmid``, ``subject_userids`` (the ids the worker needs, CLEARED when the
  record completes), ``outcome`` (``erased`` | ``failed``), ``error_code``. The subject hash
  becomes optional (an activity-wide request has none).
- Grants: the API reads and writes the privacy settings and reads the tombstones and the erasure
  records it queued; the worker reads the settings, writes the tombstones, and may now delete
  the extra-time and reset rows of the sessions it purges or erases. The API still deletes
  nothing.

Revision ID: 0040
Revises: 0039
"""

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

#: revision 0038's frozen matrix (0039 changed no privilege) plus this revision's changes, in the
#: rendered order (the integration test compares the result with
#: ``vektra_assess.db.grants.render_statements``). Written as a delta.
_REPLACE = {
    "assess_outage_incidents, assess_render_checks, ": (
        "assess_outage_incidents, assess_privacy_config, assess_purge_records, "
        "assess_render_checks, "
    ),
    "assess_outage_incidents_id_seq, assess_render_checks_id_seq": (
        "assess_outage_incidents_id_seq, assess_purge_records_id_seq, "
        "assess_render_checks_id_seq"
    ),
}
#: (the statement it follows, the new statements)
_INSERT_AFTER = (
    (
        "GRANT INSERT ON assess_erasure_records TO vektra_assess_api",
        ("GRANT SELECT ON assess_erasure_records TO vektra_assess_api",),
    ),
    (
        "GRANT SELECT ON assess_outage_incidents TO vektra_assess_api",
        (
            "GRANT INSERT ON assess_privacy_config TO vektra_assess_api",
            "GRANT SELECT ON assess_privacy_config TO vektra_assess_api",
            "GRANT UPDATE (retention_days, legal_basis_label, legal_basis_text, notice_url, "
            "notice_text, notice_version, updated_by, updated_at) ON assess_privacy_config TO "
            "vektra_assess_api",
            "GRANT SELECT ON assess_purge_records TO vektra_assess_api",
        ),
    ),
    (
        "GRANT UPDATE ON assess_erasure_records TO vektra_assess_worker",
        ("GRANT DELETE ON assess_extra_time TO vektra_assess_worker",),
    ),
    (
        "GRANT UPDATE ON assess_outage_incidents TO vektra_assess_worker",
        (
            "GRANT SELECT ON assess_privacy_config TO vektra_assess_worker",
            "GRANT INSERT ON assess_purge_records TO vektra_assess_worker",
            "GRANT SELECT ON assess_purge_records TO vektra_assess_worker",
        ),
    ),
    (
        "GRANT UPDATE ON assess_render_state TO vektra_assess_worker",
        ("GRANT DELETE ON assess_reset_grants TO vektra_assess_worker",),
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
    for stmt in _rev("0038_assess_teacher_ops.py").GRANT_STATEMENTS:
        if stmt.startswith("REVOKE ALL ON") or stmt.startswith(
            "GRANT USAGE ON SEQUENCE"
        ):
            for old, new in _REPLACE.items():
                stmt = stmt.replace(old, new)
        out.append(stmt)
        out.extend(after.get(stmt, ()))
    return tuple(out)


GRANT_STATEMENTS = (*_statements(),)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE assess_privacy_config (
            site_id            UUID PRIMARY KEY REFERENCES assess_sites(id) ON DELETE CASCADE,
            retention_days     INTEGER NOT NULL
                CONSTRAINT ck_assess_privacy_retention CHECK (retention_days BETWEEN 1 AND 3650),
            legal_basis_label  TEXT NOT NULL
                CONSTRAINT ck_assess_privacy_label CHECK (length(legal_basis_label) BETWEEN 1 AND 200),
            legal_basis_text   TEXT NOT NULL
                CONSTRAINT ck_assess_privacy_basis CHECK (length(legal_basis_text) BETWEEN 1 AND 4000),
            notice_url         TEXT NOT NULL
                CONSTRAINT ck_assess_privacy_url CHECK (
                    length(notice_url) BETWEEN 9 AND 2000 AND notice_url LIKE 'https://%'),
            notice_text        TEXT NOT NULL
                CONSTRAINT ck_assess_privacy_notice CHECK (length(notice_text) BETWEEN 1 AND 8000),
            notice_version     TEXT NOT NULL
                CONSTRAINT ck_assess_privacy_version CHECK (length(notice_version) BETWEEN 1 AND 40),
            updated_by         TEXT NOT NULL,
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE assess_purge_records (
            id             BIGSERIAL PRIMARY KEY,
            site_id        UUID NOT NULL REFERENCES assess_sites(id) ON DELETE CASCADE,
            subject_ref    TEXT NOT NULL,
            session_id     UUID NOT NULL,
            transcript_id  UUID NULL,
            session_no     INTEGER NOT NULL,
            cmid           BIGINT NOT NULL,
            reason         TEXT NOT NULL
                CONSTRAINT ck_assess_purge_records_reason CHECK (reason IN ('retention', 'erasure')),
            purged_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            counts         JSONB NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT uq_assess_purge_records_session UNIQUE (session_id)
        )
    """)
    op.execute(
        "CREATE INDEX ix_assess_purge_records_subject "
        "ON assess_purge_records (site_id, cmid, subject_ref)"
    )
    op.execute(
        "CREATE INDEX ix_assess_purge_records_transcript "
        "ON assess_purge_records (transcript_id) WHERE transcript_id IS NOT NULL"
    )
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM assess_erasure_records) THEN
                RAISE EXCEPTION 'assess_erasure_records holds rows that no release could write';
            END IF;
        END
        $$
    """)
    op.execute(
        "ALTER TABLE assess_erasure_records ALTER COLUMN subject_userid_hash DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE assess_erasure_records ADD COLUMN scope TEXT NOT NULL "
        "CONSTRAINT ck_assess_erasure_records_scope CHECK (scope IN ('subject', 'activity'))"
    )
    op.execute("ALTER TABLE assess_erasure_records ADD COLUMN cmid BIGINT NULL")
    op.execute(
        "ALTER TABLE assess_erasure_records ADD COLUMN subject_userids BIGINT[] NULL"
    )
    op.execute(
        "ALTER TABLE assess_erasure_records ADD COLUMN outcome TEXT NULL "
        "CONSTRAINT ck_assess_erasure_records_outcome CHECK (outcome IN ('erased', 'failed'))"
    )
    op.execute("ALTER TABLE assess_erasure_records ADD COLUMN error_code TEXT NULL")
    # a subject request names its subject; an activity request names its activity; the ids the
    # worker needs are gone once the record is complete
    op.execute(
        "ALTER TABLE assess_erasure_records ADD CONSTRAINT ck_assess_erasure_records_shape "
        "CHECK ((scope = 'subject' AND subject_userid_hash IS NOT NULL) "
        "OR (scope = 'activity' AND cmid IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE assess_erasure_records ADD CONSTRAINT ck_assess_erasure_records_cleared "
        "CHECK (completed_at IS NULL OR subject_userids IS NULL)"
    )
    for stmt in GRANT_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DELETE FROM assess_erasure_records")
    for name in (
        "ck_assess_erasure_records_cleared",
        "ck_assess_erasure_records_shape",
    ):
        op.execute(f"ALTER TABLE assess_erasure_records DROP CONSTRAINT {name}")
    for column in ("error_code", "outcome", "subject_userids", "cmid", "scope"):
        op.execute(f"ALTER TABLE assess_erasure_records DROP COLUMN {column}")
    op.execute(
        "ALTER TABLE assess_erasure_records ALTER COLUMN subject_userid_hash SET NOT NULL"
    )
    op.execute("DROP TABLE assess_purge_records")
    op.execute("DROP TABLE assess_privacy_config")
    for stmt in _rev("0038_assess_teacher_ops.py").GRANT_STATEMENTS:
        op.execute(stmt)

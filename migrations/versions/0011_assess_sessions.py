"""Assess sessions, snapshots and turns (plan assess-data-crypto 1.2).

DB-enforced lifecycle invariants (REQ-067, BR-015):

- one non-CLOSED session per (site, student, cmid): partial unique index;
- ``state`` and ``close_reason`` are constrained and ``CLOSED <=> close_reason``;
- a deferred constraint trigger makes "consumed == exists(opening turn)" structural:
  a committed ACTIVE/OUTAGE/CLOSED session without an ``opening`` turn is rejected;
- snapshots are immutable once written (BEFORE UPDATE trigger raises).

A rolled-back start DELETEs the STARTING row (snapshot and keys cascade) and leaves an
``assess_start_attempts`` row, so no ROLLED_BACK state pollutes the partial index.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE assess_sessions (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            site_id             UUID NOT NULL REFERENCES assess_sites(id),
            cmid                BIGINT NOT NULL,
            student_userid      BIGINT NOT NULL,
            session_no          INTEGER NOT NULL,
            attempt_ref         TEXT NULL,
            state               TEXT NOT NULL,
            close_reason        TEXT NULL,
            starting_expires_at TIMESTAMPTZ NULL,
            deadline_at         TIMESTAMPTZ NULL,
            credited_outage_s   INTEGER NOT NULL DEFAULT 0,
            seq                 INTEGER NOT NULL DEFAULT 0,
            epoch               INTEGER NOT NULL DEFAULT 1,
            notice_version      TEXT NULL,
            regrade_flag        BOOLEAN NOT NULL DEFAULT false,
            committed_at        TIMESTAMPTZ NULL,
            closed_at           TIMESTAMPTZ NULL,
            last_activity_at    TIMESTAMPTZ NULL,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_assess_sessions_state
                CHECK (state IN ('STARTING', 'ACTIVE', 'OUTAGE', 'CLOSED')),
            CONSTRAINT ck_assess_sessions_close_reason CHECK (close_reason IS NULL OR
                close_reason IN ('finished', 'timed_out', 'service_failure',
                                 'budget_exhausted', 'terminated_by_teacher',
                                 'access_revoked')),
            CONSTRAINT ck_assess_sessions_closed_has_reason
                CHECK ((state = 'CLOSED') = (close_reason IS NOT NULL)),
            CONSTRAINT ck_assess_sessions_credited_outage CHECK (credited_outage_s >= 0),
            CONSTRAINT uq_assess_sessions_no
                UNIQUE (site_id, student_userid, cmid, session_no),
            CONSTRAINT uq_assess_sessions_id_state UNIQUE (id, state)
        )
    """)
    op.execute("""
        CREATE FUNCTION assess_sessions_number() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            -- session #N+1 after a reset (REQ-087): number it under a lock on the triple,
            -- so no caller has to compute max()+1 itself
            IF NEW.session_no IS NULL THEN
                PERFORM pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        NEW.site_id::text || ':' || NEW.student_userid || ':' || NEW.cmid, 0));
                SELECT coalesce(max(s.session_no), 0) + 1 INTO NEW.session_no
                FROM public.assess_sessions s
                WHERE s.site_id = NEW.site_id AND s.student_userid = NEW.student_userid
                  AND s.cmid = NEW.cmid;
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_assess_sessions_number BEFORE INSERT ON assess_sessions
            FOR EACH ROW EXECUTE FUNCTION assess_sessions_number()
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_assess_sessions_one_open
            ON assess_sessions (site_id, student_userid, cmid) WHERE state <> 'CLOSED'
    """)
    op.execute("""
        CREATE TABLE assess_start_attempts (
            id              BIGSERIAL PRIMARY KEY,
            site_id         UUID NOT NULL REFERENCES assess_sites(id),
            cmid            BIGINT NOT NULL,
            student_userid  BIGINT NOT NULL,
            outcome         TEXT NOT NULL,
            reason_code     TEXT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_assess_start_attempts_outcome
                CHECK (outcome IN ('committed', 'rolled_back', 'refused'))
        )
    """)
    op.execute("""
        CREATE TABLE assess_outage_incidents (
            id           BIGSERIAL PRIMARY KEY,
            session_id   UUID NOT NULL REFERENCES assess_sessions(id) ON DELETE CASCADE,
            started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            ended_at     TIMESTAMPTZ NULL,
            reason_code  TEXT NULL
        )
    """)
    op.execute("""
        CREATE TABLE assess_enrolment_checks (
            id          BIGSERIAL PRIMARY KEY,
            session_id  UUID NOT NULL REFERENCES assess_sessions(id) ON DELETE CASCADE,
            checked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            result      TEXT NOT NULL,
            CONSTRAINT ck_assess_enrolment_checks_result
                CHECK (result IN ('enrolled', 'not_enrolled', 'unreadable'))
        )
    """)
    op.execute("""
        CREATE TABLE assess_snapshots (
            session_id    UUID PRIMARY KEY REFERENCES assess_sessions(id) ON DELETE CASCADE,
            ciphertext    BYTEA NOT NULL,
            content_hash  TEXT NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE FUNCTION assess_snapshots_immutable() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            RAISE EXCEPTION 'assess_snapshots rows are immutable (BR-015)'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_assess_snapshots_immutable
            BEFORE UPDATE ON assess_snapshots
            FOR EACH ROW EXECUTE FUNCTION assess_snapshots_immutable()
    """)
    op.execute("""
        CREATE TABLE assess_turns (
            session_id    UUID NOT NULL REFERENCES assess_sessions(id) ON DELETE CASCADE,
            turn_no       INTEGER NOT NULL,
            role          TEXT NOT NULL,
            kind          TEXT NOT NULL,
            ciphertext    BYTEA NOT NULL,
            turn_hash     TEXT NOT NULL,
            prev_hash     TEXT NOT NULL,
            marker_flags  INTEGER NOT NULL DEFAULT 0,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (session_id, turn_no),
            CONSTRAINT ck_assess_turns_kind
                CHECK (kind IN ('opening', 'normal', 'interrupted', 'system')),
            CONSTRAINT ck_assess_turns_role CHECK (role IN ('student', 'bot', 'system')),
            CONSTRAINT ck_assess_turns_no CHECK (turn_no >= 1)
        )
    """)
    op.execute("""
        CREATE FUNCTION assess_sessions_require_opening_turn() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NEW.state IN ('ACTIVE', 'OUTAGE', 'CLOSED') AND NOT EXISTS (
                SELECT 1 FROM public.assess_turns t
                WHERE t.session_id = NEW.id AND t.kind = 'opening'
            ) THEN
                RAISE EXCEPTION
                    'assess session % is % without an opening turn (REQ-067)',
                    NEW.id, NEW.state
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NULL;
        END
        $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_assess_sessions_opening_turn
            AFTER INSERT OR UPDATE OF state ON assess_sessions
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION assess_sessions_require_opening_turn()
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM assess_sessions) THEN
                RAISE EXCEPTION 'migration 0011 downgrade refused: assess_sessions holds '
                    'rows (exam evidence). Export and purge them first.';
            END IF;
        END
        $$
    """)
    op.execute("DROP TABLE assess_turns")
    op.execute("DROP TABLE assess_snapshots")
    op.execute("DROP FUNCTION assess_snapshots_immutable()")
    op.execute("DROP TABLE assess_enrolment_checks")
    op.execute("DROP TABLE assess_outage_incidents")
    op.execute("DROP TABLE assess_start_attempts")
    op.execute("DROP TABLE assess_sessions")
    op.execute("DROP FUNCTION assess_sessions_require_opening_turn()")
    op.execute("DROP FUNCTION assess_sessions_number()")

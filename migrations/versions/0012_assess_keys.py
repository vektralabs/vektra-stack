"""Assess session keys and hand-off rows (plan assess-data-crypto 1.3).

- ``assess_session_keys``: the per-session DEK wrapped under the KEK (worker only).
  ``kek_wrapped IS NULL`` exactly when ``shredded_at IS NOT NULL`` (crypto-shredding).
- ``assess_session_handoff``: the DEK sealed to the API's public key. The composite FK
  ``(session_id, session_state) -> assess_sessions(id, state) ON UPDATE CASCADE`` plus
  ``CHECK (session_state IN ('ACTIVE','OUTAGE'))`` is the ARCH-079 "CHECK": a hand-off row
  for a non-live session cannot be inserted, and a session cannot reach CLOSED while its
  hand-off row survives.
- A BEFORE UPDATE trigger deletes the hand-off row when a session becomes CLOSED. ARCH-079
  says AFTER UPDATE; BEFORE runs ahead of the FK cascade, so a forgotten application-side
  delete is defended instead of failing the close.
- Role ``vektra_assess_definer`` (NOLOGIN) owns the SECURITY DEFINER functions (task 2).

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_ROLE_ATTRS = "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS NOINHERIT"


def upgrade() -> None:
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vektra_assess_definer') THEN
                BEGIN
                    CREATE ROLE vektra_assess_definer {_ROLE_ATTRS};
                EXCEPTION WHEN insufficient_privilege THEN
                    RAISE EXCEPTION
                        'migration 0012: cannot create role vektra_assess_definer (the '
                        'migration user lacks CREATEROLE). Pre-create it as {_ROLE_ATTRS} '
                        'and re-run the migration.';
                END;
            END IF;
        END
        $$
    """)
    op.execute("""
        CREATE TABLE assess_session_keys (
            session_id   UUID PRIMARY KEY REFERENCES assess_sessions(id) ON DELETE CASCADE,
            kek_wrapped  BYTEA NULL,
            kek_kid      TEXT NOT NULL,
            key_version  INTEGER NOT NULL DEFAULT 1,
            shredded_at  TIMESTAMPTZ NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_assess_session_keys_shredded
                CHECK ((kek_wrapped IS NULL) = (shredded_at IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE TABLE assess_session_handoff (
            session_id     UUID PRIMARY KEY,
            session_state  TEXT NOT NULL,
            epoch          INTEGER NOT NULL,
            sealed_dek     BYTEA NOT NULL,
            handoff_kid    TEXT NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_assess_session_handoff_live
                CHECK (session_state IN ('ACTIVE', 'OUTAGE')),
            CONSTRAINT fk_assess_session_handoff_session
                FOREIGN KEY (session_id, session_state)
                REFERENCES assess_sessions (id, state)
                ON UPDATE CASCADE ON DELETE CASCADE
        )
    """)
    op.execute("""
        CREATE FUNCTION assess_sessions_drop_handoff_on_close() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NEW.state = 'CLOSED' THEN
                DELETE FROM public.assess_session_handoff WHERE session_id = NEW.id;
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_assess_sessions_drop_handoff
            BEFORE UPDATE OF state ON assess_sessions
            FOR EACH ROW EXECUTE FUNCTION assess_sessions_drop_handoff_on_close()
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM assess_sessions) THEN
                RAISE EXCEPTION 'migration 0012 downgrade refused: assess_sessions holds '
                    'rows (exam evidence). Export and purge them first.';
            END IF;
        END
        $$
    """)
    op.execute("DROP TRIGGER trg_assess_sessions_drop_handoff ON assess_sessions")
    op.execute("DROP FUNCTION assess_sessions_drop_handoff_on_close()")
    op.execute("DROP TABLE assess_session_handoff")
    op.execute("DROP TABLE assess_session_keys")
    # the role is cluster-level and shared with other databases: left in place

"""Assess scopes and Postgres roles (plan assess-foundations 4.3).

Two things the assessment discussion feature needs before any of its tables exist:

1. Four site-bound API key scopes: ``assess:mint``, ``assess:teacher``,
   ``assess:privacy-export``, ``assess:privacy-erase``. They are NOT satisfied by
   ``admin`` (``require_exact_scope``), and ``ck_api_keys_assess_scope_exclusive``
   forbids a key from holding an assess scope together with ``admin``, ``ingest`` or
   ``query``: the Moodle plugin's key is useless as a Vektra admin key, and the
   database, not only the admin UI, says so (ARCH-067).

2. The ``vektra_assess_api`` and ``vektra_assess_worker`` roles, NOLOGIN here. Fresh
   volumes get them from ``deploy/postgres/init.sql``; this creates them on existing
   volumes, where init.sql never re-runs. LOGIN and passwords are set by the one-shot
   ``vektra-assess-db-init`` service from secret files, never by a migration. Grants
   belong to a later migration (plan assess-data-crypto).

Downgrade refuses while any key holds an assess scope (dropping the constraint would
otherwise make those rows violate the restored one, or lose them silently). The roles
are cluster-level objects shared with init.sql and are left in place on downgrade.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_SEVEN_SCOPES = (
    "ARRAY['admin','ingest','query','assess:mint','assess:teacher',"
    "'assess:privacy-export','assess:privacy-erase']::text[]"
)
_LEGACY_SCOPES = "ARRAY['admin','ingest','query']::text[]"

_ROLE_ATTRS = "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS NOINHERIT"


def upgrade() -> None:
    op.execute("ALTER TABLE api_keys DROP CONSTRAINT ck_api_keys_scopes")
    op.execute(
        f"ALTER TABLE api_keys ADD CONSTRAINT ck_api_keys_scopes "
        f"CHECK (scopes <@ {_SEVEN_SCOPES})"
    )
    # A key holding any assess:* scope holds none of the legacy scopes.
    op.execute(
        f"ALTER TABLE api_keys ADD CONSTRAINT ck_api_keys_assess_scope_exclusive "
        f"CHECK (NOT (scopes && ARRAY['assess:mint','assess:teacher',"
        f"'assess:privacy-export','assess:privacy-erase']::text[]) "
        f"OR NOT (scopes && {_LEGACY_SCOPES}))"
    )
    op.execute(f"""
        DO $$
        DECLARE
            r text;
        BEGIN
            FOREACH r IN ARRAY ARRAY['vektra_assess_api', 'vektra_assess_worker'] LOOP
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
                    BEGIN
                        EXECUTE format('CREATE ROLE %I {_ROLE_ATTRS}', r);
                    EXCEPTION WHEN insufficient_privilege THEN
                        RAISE EXCEPTION
                            'migration 0009: cannot create role % (the migration user '
                            'lacks CREATEROLE). On managed Postgres, pre-create all three '
                            'assess roles, vektra_assess_api, vektra_assess_worker and '
                            'vektra_assess_definer, as {_ROLE_ATTRS}; make the migration '
                            'user a member of vektra_assess_definer, so that it can '
                            'SET ROLE vektra_assess_definer (revision 0014 needs it); '
                            'then re-run the migration.', r;
                    END;
                END IF;
            END LOOP;
        END
        $$
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        DECLARE
            n integer;
        BEGIN
            SELECT count(*) INTO n FROM api_keys WHERE scopes && ARRAY['assess:mint',
                'assess:teacher', 'assess:privacy-export', 'assess:privacy-erase']::text[];
            IF n > 0 THEN
                RAISE EXCEPTION
                    'migration 0009 downgrade refused: % API key(s) hold an assess:* '
                    'scope. Revoke and delete them first.', n;
            END IF;
        END
        $$
    """)
    op.execute(
        "ALTER TABLE api_keys DROP CONSTRAINT ck_api_keys_assess_scope_exclusive"
    )
    op.execute("ALTER TABLE api_keys DROP CONSTRAINT ck_api_keys_scopes")
    op.execute(
        f"ALTER TABLE api_keys ADD CONSTRAINT ck_api_keys_scopes "
        f"CHECK (scopes <@ {_LEGACY_SCOPES})"
    )

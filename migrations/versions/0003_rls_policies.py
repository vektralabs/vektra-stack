"""RLS policies for namespace isolation (ADR-0009, ARCH-025).

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-01

Enables Row-Level Security on namespace-scoped tables and creates
permissive policies using current_setting('app.current_namespace').

Policies are inert until the application calls:
    SET LOCAL app.current_namespace = :namespace_id

When VEKTRA_MULTI_TENANT=false (Phase 1 default), the SET LOCAL is never
called and RLS has no effect. When true, the RLS middleware sets the
session variable before each query.

Tables with RLS enabled:
  - source_documents (namespace_id)
  - document_chunks (namespace_id)
  - ingest_jobs (namespace_id)
  - conversations (namespace_id)
  - conversation_turns (via conversations FK, no direct RLS needed)
  - feedback (namespace_id)
  - query_traces (namespace_id)
"""

from __future__ import annotations

from alembic import op

# revision identifiers
revision: str = "0003"
down_revision: str = "0002"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Tables that have namespace_id and need RLS
_RLS_TABLES = [
    "source_documents",
    "document_chunks",
    "ingest_jobs",
    "conversations",
    "feedback",
    "query_traces",
]


def upgrade() -> None:
    for table in _RLS_TABLES:
        # Enable RLS (does nothing until policies are created)
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

        # Force RLS for table owner too (important for superuser connections)
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

        # Permissive SELECT/UPDATE/DELETE policy.
        # When app.current_namespace is unset (empty string), all rows are
        # visible -- this preserves Phase 1 single-tenant behavior even with
        # FORCE ROW LEVEL SECURITY enabled.
        op.execute(f"""
            CREATE POLICY {table}_namespace_isolation ON {table}
                USING (
                    current_setting('app.current_namespace', true) = ''
                    OR namespace_id = current_setting('app.current_namespace', true)
                )
        """)

        # WITH CHECK for INSERT (validates new rows match current namespace).
        # When unset (single-tenant), any namespace_id is accepted.
        op.execute(f"""
            CREATE POLICY {table}_namespace_insert ON {table}
                FOR INSERT
                WITH CHECK (
                    current_setting('app.current_namespace', true) = ''
                    OR namespace_id = current_setting('app.current_namespace', true)
                )
        """)


def downgrade() -> None:
    for table in reversed(_RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_namespace_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_namespace_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

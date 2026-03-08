"""Add 'pipeline_failure' to source_documents deletion_reason CHECK constraint.

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-08

The _cleanup_document() function in vektra-ingest sets
deletion_reason='pipeline_failure' for documents that fail during ingestion.
The original CHECK constraint (0001) only allowed 'user_request',
'superseded', and 'expired'.
"""

from __future__ import annotations

from alembic import op

# revision identifiers
revision: str = "0005"
down_revision: str = "0004"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_source_documents_deletion_reason", "source_documents", type_="check"
    )
    op.create_check_constraint(
        "ck_source_documents_deletion_reason",
        "source_documents",
        "deletion_reason IS NULL OR deletion_reason IN "
        "('user_request', 'superseded', 'expired', 'pipeline_failure')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_source_documents_deletion_reason", "source_documents", type_="check"
    )
    op.create_check_constraint(
        "ck_source_documents_deletion_reason",
        "source_documents",
        "deletion_reason IS NULL OR deletion_reason IN "
        "('user_request', 'superseded', 'expired')",
    )

"""Add 'pipeline_failure' to source_documents deletion_reason CHECK.

Environments where 0001 was already applied have the original CHECK
constraint without 'pipeline_failure'. This migration drops and recreates
the constraint to include the new value.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE source_documents
            DROP CONSTRAINT IF EXISTS ck_source_documents_deletion_reason
    """)
    op.execute("""
        ALTER TABLE source_documents
            ADD CONSTRAINT ck_source_documents_deletion_reason
            CHECK (deletion_reason IS NULL OR deletion_reason IN
                   ('user_request', 'superseded', 'expired', 'pipeline_failure'))
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE source_documents
            DROP CONSTRAINT IF EXISTS ck_source_documents_deletion_reason
    """)
    op.execute("""
        ALTER TABLE source_documents
            ADD CONSTRAINT ck_source_documents_deletion_reason
            CHECK (deletion_reason IS NULL OR deletion_reason IN
                   ('user_request', 'superseded', 'expired'))
    """)

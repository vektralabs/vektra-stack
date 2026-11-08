"""Add an owner to conversations.

A conversation recorded which API key created it (`key_id`) and which namespace
it belongs to, and nothing else. Learn-originated conversations all carry the
same sentinel key_id, because the widget authenticates with a JWT and holds no
API key — so within one course every conversation looked identical, and the
only authorization the turns endpoint could perform was "same namespace".
Any student of a course could therefore read another student's turns given a
conversation id (FEAT-027).

`owner_subject` stores the JWT `sub` claim: the LMS user identifier the token
was issued for. Nullable, because conversations created through the core API
have an API key rather than a subject, and rows predating this migration have
no owner that could be reconstructed.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("owner_subject", sa.String(255), nullable=True),
    )
    # Supports the only query that reads it: "this student's conversations in
    # this course, most recent first". Partial, because a soft-deleted
    # conversation is never listed (REQ-057).
    op.execute("""
        CREATE INDEX ix_conversations_owner
            ON conversations (namespace_id, owner_subject, updated_at DESC)
            WHERE deleted_at IS NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_conversations_owner", table_name="conversations")
    op.drop_column("conversations", "owner_subject")

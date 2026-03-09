"""E-learning vertical: enrollments and dashboard_tokens tables.

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-09

Adds:
  - enrollments table: student-course binding with namespace reference.
  - dashboard_tokens table: short-lived JWT tracking for chatbot widget auth.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0005"
down_revision: str = "0004"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # Enrollment: student-course binding
    op.create_table(
        "enrollments",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("student_id", sa.String(255), nullable=False),
        sa.Column("course_id", sa.String(255), nullable=False),
        sa.Column(
            "namespace",
            sa.String(64),
            sa.ForeignKey("namespaces.id"),
            nullable=False,
        ),
        sa.Column(
            "enrolled_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "metadata",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.UniqueConstraint("student_id", "course_id", name="uq_enrollment_student_course"),
    )

    # Dashboard token: short-lived JWT for widget auth
    op.create_table(
        "dashboard_tokens",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("student_id", sa.String(255), nullable=False),
        sa.Column("course_id", sa.String(255), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_dashboard_tokens_hash",
        "dashboard_tokens",
        ["token_hash"],
    )


def downgrade() -> None:
    op.drop_index("idx_dashboard_tokens_hash")
    op.drop_table("dashboard_tokens")
    op.drop_table("enrollments")

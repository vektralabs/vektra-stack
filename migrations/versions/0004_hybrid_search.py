"""Hybrid search: sparse_vector column + reindex_jobs table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-02

Adds:
  - sparse_vector JSONB column on document_chunks (nullable, default NULL).
    Stores {"indices": [int, ...], "values": [float, ...]} for BM25/SPLADE.
    Existing Phase 1 chunks keep NULL and work with DENSE mode only.
  - reindex_jobs table for zero-downtime reindex tracking (ARCH-045, REQ-064).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0004"
down_revision: str = "0003"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # Sparse vector column on document_chunks
    op.add_column(
        "document_chunks",
        sa.Column("sparse_vector", sa.dialects.postgresql.JSONB(), nullable=True),
    )

    # Reindex jobs tracking table (ARCH-045, REQ-064)
    op.create_table(
        "reindex_jobs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "namespace_id",
            sa.String(64),
            sa.ForeignKey("namespaces.id"),
            nullable=False,
        ),
        sa.Column("source_index_version", sa.INTEGER(), nullable=False),
        sa.Column("target_index_version", sa.INTEGER(), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "total_documents", sa.INTEGER(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "processed_documents",
            sa.INTEGER(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "current_document_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("error_message", sa.TEXT(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_reindex_jobs_status",
        ),
    )
    op.create_index(
        "ix_reindex_jobs_namespace_status",
        "reindex_jobs",
        ["namespace_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_reindex_jobs_namespace_status")
    op.drop_table("reindex_jobs")
    op.drop_column("document_chunks", "sparse_vector")

"""Namespace quota enforcement (ARCH-047).

Checks namespace document and chunk counts against configured quotas
before allowing ingest operations. Called at the API boundary (fail fast).

Uses raw SQL to count documents/chunks to avoid importing from other
vektra components (ADR-0005 module boundaries).
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_shared.errors import (
    ERR_QUOTA_001,
    ErrorCategory,
    ErrorResponse,
    http_status_for,
)


async def check_namespace_quota(
    session: AsyncSession,
    namespace_id: str,
    new_documents: int = 0,
    new_chunks: int = 0,
) -> None:
    if new_documents < 0 or new_chunks < 0:
        raise ValueError("Quota deltas must be non-negative")
    """Raise HTTPException 422 if adding would exceed namespace quotas.

    Args:
        session: active database session
        namespace_id: target namespace
        new_documents: number of documents being added
        new_chunks: number of chunks being added (0 if unknown at ingest time)

    Raises:
        HTTPException with ERR-QUOTA-001 if quota would be exceeded.
        Does nothing if quotas are NULL (unlimited).
    """
    from vektra_admin.models import NamespaceOrm  # late import

    # Fetch quota limits
    result = await session.execute(
        select(
            NamespaceOrm.quota_documents,
            NamespaceOrm.quota_chunks,
        ).where(NamespaceOrm.id == namespace_id)
    )
    row = result.one_or_none()
    if row is None:
        return  # namespace not found, let other validation handle it

    quota_documents, quota_chunks = row.quota_documents, row.quota_chunks

    # Both unlimited: skip counting
    if quota_documents is None and quota_chunks is None:
        return

    # Check document quota (raw SQL to avoid cross-module imports)
    if quota_documents is not None and new_documents > 0:
        count_result = await session.execute(
            text(
                "SELECT count(*) FROM source_documents "
                "WHERE namespace_id = :ns AND deleted_at IS NULL"
            ),
            {"ns": namespace_id},
        )
        current_docs = count_result.scalar_one()
        if current_docs + new_documents > quota_documents:
            err = ErrorResponse(
                category=ErrorCategory.PERMANENT,
                code=ERR_QUOTA_001,
                message=(
                    f"Document quota exceeded for namespace '{namespace_id}': "
                    f"{current_docs} existing + {new_documents} new > {quota_documents} limit."
                ),
                remediation="Delete existing documents or increase the namespace quota.",
            )
            raise HTTPException(
                status_code=http_status_for(err), detail=err.to_envelope()
            )

    # Check chunk quota (raw SQL to avoid cross-module imports)
    if quota_chunks is not None and new_chunks > 0:
        count_result = await session.execute(
            text("SELECT count(*) FROM document_chunks WHERE namespace_id = :ns"),
            {"ns": namespace_id},
        )
        current_chunks = count_result.scalar_one()
        if current_chunks + new_chunks > quota_chunks:
            err = ErrorResponse(
                category=ErrorCategory.PERMANENT,
                code=ERR_QUOTA_001,
                message=(
                    f"Chunk quota exceeded for namespace '{namespace_id}': "
                    f"{current_chunks} existing + {new_chunks} new > {quota_chunks} limit."
                ),
                remediation="Delete existing documents or increase the namespace quota.",
            )
            raise HTTPException(
                status_code=http_status_for(err), detail=err.to_envelope()
            )

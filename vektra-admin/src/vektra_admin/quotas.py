"""Namespace quota enforcement (ARCH-047).

Checks namespace document and chunk counts against configured quotas
before allowing ingest operations. Called at the API boundary (fail fast).

Document counts use raw SQL against ``source_documents``, a provider-neutral
table. Chunk counts go through the ``VectorStoreProvider`` Protocol
(``count_chunks``): ``document_chunks`` is private to the pgvector provider
(ADR-0026), so counting it directly reads an empty table under any other
provider (e.g. Qdrant) and would silently never enforce the chunk quota. The
provider is injected by the caller, so vektra-admin stays within its module
boundary (ADR-0005): it depends only on the vektra_shared Protocol, not on
vektra-index.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_shared.errors import (
    ERR_QUOTA_001,
    ErrorCategory,
    ErrorResponse,
    http_status_for,
)

if TYPE_CHECKING:
    from vektra_shared.protocols import VectorStoreProvider


async def check_namespace_quota(
    session: AsyncSession,
    namespace_id: str,
    new_documents: int = 0,
    new_chunks: int = 0,
    *,
    vector_store: VectorStoreProvider | None = None,
) -> None:
    """Raise HTTPException 422 if adding would exceed namespace quotas.

    Args:
        session: active database session
        namespace_id: target namespace
        new_documents: number of documents being added
        new_chunks: number of chunks being added (0 if unknown at ingest time)
        vector_store: active vector store provider, required only when a chunk
            quota is set and ``new_chunks > 0``. Resolve it from the provider
            registry at the call site. The chunk count is the number of points
            in the store for the namespace at its active index version, which
            ``document_chunks`` cannot report under a non-pgvector provider
            (ADR-0026).

    Raises:
        HTTPException with ERR-QUOTA-001 if quota would be exceeded.
        ValueError if a chunk quota must be enforced but ``vector_store`` is None.
        Does nothing if quotas are NULL (unlimited).
    """
    if new_documents < 0 or new_chunks < 0:
        raise ValueError("Quota deltas must be non-negative")

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

    # Check document quota (raw SQL: source_documents is provider-neutral)
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

    # Check chunk quota via the vector store: document_chunks is private to the
    # pgvector provider (ADR-0026), so raw SQL against it would count 0 under
    # Qdrant and never enforce the quota.
    if quota_chunks is not None and new_chunks > 0:
        if vector_store is None:
            raise ValueError(
                "vector_store is required to enforce the chunk quota; "
                "resolve it from the provider registry at the call site"
            )
        current_chunks = await vector_store.count_chunks(namespace_id)
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

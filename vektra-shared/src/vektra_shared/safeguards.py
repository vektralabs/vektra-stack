"""PassthroughSafeguard: Phase 1 default SafeguardHook implementation.

All three trust boundary points (pre_query, post_retrieval, pre_response)
pass through unchanged with <5ms overhead. No content is blocked or modified.

Phase 2: Presidio-based PII anonymization (modified_content), Guardrails AI
for output validation.
"""
from __future__ import annotations

from vektra_shared.types import SafeguardContext, SafeguardResult, SearchResult


class PassthroughSafeguard:
    """SafeguardHook that allows all requests through without modification.

    Implements the SafeguardHook Protocol. Safe to use in development and
    in deployments where safeguards are not yet configured.
    """

    async def pre_query(
        self,
        query_ref: str,
        context: SafeguardContext,
    ) -> SafeguardResult:
        """Allow all queries through."""
        return SafeguardResult(allowed=True)

    async def post_retrieval(
        self,
        query_ref: str,
        results: list[SearchResult],
        context: SafeguardContext,
    ) -> SafeguardResult:
        """Allow all retrieval results through without filtering."""
        return SafeguardResult(allowed=True)

    async def pre_response(
        self,
        response_ref: str,
        context: SafeguardContext,
    ) -> SafeguardResult:
        """Allow all responses through without modification."""
        return SafeguardResult(allowed=True)

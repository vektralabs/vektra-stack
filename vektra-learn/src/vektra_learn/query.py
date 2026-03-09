"""Course-scoped query adapter for the e-learning vertical.

Provides helpers to build namespace-scoped QueryRequest objects with
course_id metadata filters, and to convert QueryResponse to the
course-facing response shape. JWT validation is handled by api.py.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from pydantic import BaseModel

from vektra_shared.types import QueryRequest, QueryResponse

log = structlog.get_logger(__name__)


class CourseQueryRequest(BaseModel):
    """Request for a course-scoped RAG query."""

    question: str
    conversation_id: UUID | None = None
    top_k: int = 5
    stream: bool = False


class CourseQueryResponse(BaseModel):
    """Response from a course-scoped RAG query."""

    response_id: UUID
    answer: str | None
    sources: list[dict[str, Any]]
    conversation_id: UUID | None
    no_relevant_context: bool = False


def build_course_query(
    req: CourseQueryRequest,
    namespace: str,
    course_id: str,
) -> QueryRequest:
    """Build a QueryRequest scoped to a course namespace with metadata filters.

    The course_id is injected as a metadata filter so that only chunks
    belonging to this course are retrieved (ARCH-044 JSONB filtering).
    """
    return QueryRequest(
        question=req.question,
        namespace=namespace,
        conversation_id=req.conversation_id,
        top_k=req.top_k,
        stream=req.stream,
        filters={"course_id": course_id},
    )


def pipeline_response_to_course_response(
    resp: QueryResponse,
) -> CourseQueryResponse:
    """Convert a QueryResponse to a CourseQueryResponse for the learn API."""
    return CourseQueryResponse(
        response_id=resp.response_id,
        answer=resp.answer,
        sources=[
            {
                "doc_id": str(s.doc_id),
                "chunk_id": s.chunk_id,
                "score": s.score,
                "snippet": s.snippet,
            }
            for s in resp.sources
        ],
        conversation_id=resp.conversation_id,
        no_relevant_context=resp.no_relevant_context,
    )

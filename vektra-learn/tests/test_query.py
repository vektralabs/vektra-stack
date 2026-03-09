"""Unit tests for course-scoped query wrapper: namespace injection, metadata filters."""

from __future__ import annotations

from uuid import uuid4

from vektra_learn.query import (
    CourseQueryRequest,
    build_course_query,
    pipeline_response_to_course_response,
)
from vektra_shared.types import QueryResponse, SourceRef


class TestBuildCourseQuery:
    def test_injects_namespace(self):
        req = CourseQueryRequest(question="What is ML?")
        query = build_course_query(req, namespace="corso-ml-2026", course_id="CS101")

        assert query.namespace == "corso-ml-2026"
        assert query.question == "What is ML?"

    def test_injects_course_id_filter(self):
        req = CourseQueryRequest(question="What is ML?")
        query = build_course_query(req, namespace="ns", course_id="CS101")

        assert query.filters is not None
        assert query.filters["course_id"] == "CS101"

    def test_preserves_conversation_id(self):
        cid = uuid4()
        req = CourseQueryRequest(question="Follow up", conversation_id=cid)
        query = build_course_query(req, namespace="ns", course_id="CS101")

        assert query.conversation_id == cid

    def test_preserves_top_k(self):
        req = CourseQueryRequest(question="Q", top_k=10)
        query = build_course_query(req, namespace="ns", course_id="CS101")

        assert query.top_k == 10

    def test_preserves_stream(self):
        req = CourseQueryRequest(question="Q", stream=True)
        query = build_course_query(req, namespace="ns", course_id="CS101")

        assert query.stream is True


class TestPipelineResponseToCourseResponse:
    def test_converts_response(self):
        doc_id = uuid4()
        cit_id = uuid4()
        resp_id = uuid4()
        conv_id = uuid4()

        resp = QueryResponse(
            response_id=resp_id,
            answer="Machine learning is...",
            sources=[
                SourceRef(
                    doc_id=doc_id,
                    chunk_id="chunk-1",
                    score=0.95,
                    snippet="ML is a subset...",
                    citation_id=cit_id,
                ),
            ],
            conversation_id=conv_id,
        )

        result = pipeline_response_to_course_response(resp)

        assert result.response_id == resp_id
        assert result.answer == "Machine learning is..."
        assert result.conversation_id == conv_id
        assert len(result.sources) == 1
        assert result.sources[0]["doc_id"] == str(doc_id)
        assert result.sources[0]["chunk_id"] == "chunk-1"
        assert result.sources[0]["score"] == 0.95

    def test_handles_no_relevant_context(self):
        resp = QueryResponse(
            response_id=uuid4(),
            answer=None,
            sources=[],
            conversation_id=None,
            no_relevant_context=True,
        )

        result = pipeline_response_to_course_response(resp)

        assert result.answer is None
        assert result.no_relevant_context is True
        assert result.sources == []

    def test_handles_empty_sources(self):
        resp = QueryResponse(
            response_id=uuid4(),
            answer="Some answer",
            sources=[],
            conversation_id=None,
        )

        result = pipeline_response_to_course_response(resp)
        assert result.sources == []

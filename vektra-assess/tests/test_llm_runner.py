"""DiscussionTurnRunner (plan assess-llm-runner, task A)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from test_context_bundle import _frozen
from test_context_snapshot import make

from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.assemble import assemble_context
from vektra_assess.llm import DiscussionTurnRunner, RagNotAvailableError
from vektra_shared.testing_recorders import RecordingLLMProvider
from vektra_shared.types import QueryTrace


class _Trace:
    def __init__(self) -> None:
        self.rows: list[tuple[QueryTrace, str]] = []

    async def write(self, trace: QueryTrace, namespace: str) -> None:
        self.rows.append((trace, namespace))


class _Audit:
    def __init__(self) -> None:
        self.rows: list[tuple[str, Mapping[str, Any]]] = []

    async def __call__(self, event: str, data: Mapping[str, Any]) -> None:
        self.rows.append((event, data))


class _Safeguard:
    async def pre_query(self, query: str, context: Any) -> Any: ...
    async def post_retrieval(self, chunks: Any, context: Any) -> Any: ...
    async def pre_response(self, response: str, context: Any) -> Any: ...


def settings(**kw: Any) -> VektraAssessSettings:
    return VektraAssessSettings.model_validate(
        {"VEKTRA_ASSESS_LLM_MODEL": "test/model", **kw}
    )


def runner(**kw: Any) -> DiscussionTurnRunner:
    return DiscussionTurnRunner(
        llm=RecordingLLMProvider(["ok"]),
        safeguard=_Safeguard(),
        trace_writer=_Trace(),
        settings=settings(**kw),
        audit=_Audit(),
    )


def test_constructs_from_injected_parts_only() -> None:
    r = runner()
    assert r.model == "test/model" and len(r.prompt_version) == 8


def test_refuses_to_start_without_a_model() -> None:
    with pytest.raises(ValueError, match="VEKTRA_ASSESS_LLM_MODEL"):
        DiscussionTurnRunner(
            llm=RecordingLLMProvider([]),
            safeguard=_Safeguard(),
            trace_writer=_Trace(),
            settings=VektraAssessSettings(),
            audit=_Audit(),
        )


def test_a_bundle_frozen_with_course_material_is_refused() -> None:
    s = make()
    s = s.model_copy(
        update={
            "header": s.header.model_copy(
                update={
                    "flags": s.header.flags.model_copy(update={"course_material": True})
                }
            )
        }
    )
    bundle = assemble_context(s, _frozen(s))
    assert bundle.course_material is True
    with pytest.raises(RagNotAvailableError):
        DiscussionTurnRunner._admit(bundle)


def test_an_ordinary_bundle_is_admitted() -> None:
    s = make()
    DiscussionTurnRunner._admit(assemble_context(s, _frozen(s)))

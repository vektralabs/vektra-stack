"""Structural NFR-015 evidence for the discussion runner (plan assess-llm-runner, task D).

RAG is OFF in v1 and the runner ships no retrieval branch at all: an AST scan of
``vektra_assess.llm`` finds no name, attribute, import or string that reaches a vector store or the
``post_retrieval`` safeguard hook; a bundle frozen with course material is refused before anything
is rendered; and a run never calls ``post_retrieval`` (the recording safeguard raises if it does).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fakes import RecordingAuditSink, RecordingSafeguard, RecordingTraceWriter
from test_context_bundle import _frozen
from test_context_snapshot import make

from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.assemble import assemble_context
from vektra_assess.llm import DiscussionTurnRunner, RagNotAvailableError, StudentTurn
from vektra_shared.testing_recorders import RecordingLLMProvider

PKG = Path(__file__).resolve().parents[1] / "src" / "vektra_assess" / "llm"
FORBIDDEN = ("VectorStoreProvider", "vector_store", "post_retrieval")


def _references(tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.append(node.id)
        elif isinstance(node, ast.Attribute):
            out.append(node.attr)
        elif isinstance(node, ast.alias):
            out.append(node.name)
            out.append(node.asname or "")
        elif isinstance(node, ast.ImportFrom):
            out.append(node.module or "")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)  # getattr(x, "post_retrieval") is a reference too
    return out


def _hits(source: str) -> list[str]:
    refs = _references(ast.parse(source))
    return sorted({f for f in FORBIDDEN for r in refs if f in r})


def test_the_llm_package_never_references_retrieval() -> None:
    files = sorted(PKG.rglob("*.py"))
    assert len(files) >= 10  # the scan really covers the package
    found = {
        str(p.relative_to(PKG)): _hits(p.read_text(encoding="utf-8")) for p in files
    }
    assert {k: v for k, v in found.items() if v} == {}


@pytest.mark.parametrize(
    "planted",
    [
        "from vektra_shared.protocols import VectorStoreProvider\n",
        "async def f(s, c):\n    await s.post_retrieval('q', [], c)\n",
        "def f(registry):\n    return registry.get('vector_store', 'default')\n",
        "def f(s):\n    return getattr(s, 'post_retrieval')\n",
    ],
)
def test_canary_the_scan_catches_a_planted_reference(planted: str) -> None:
    assert _hits(planted)


async def test_a_course_material_bundle_is_refused_before_any_call() -> None:
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
    llm, guard = RecordingLLMProvider(["x"]), RecordingSafeguard()
    r = DiscussionTurnRunner(
        llm=llm,
        safeguard=guard,
        trace_writer=RecordingTraceWriter(),
        settings=VektraAssessSettings.model_validate(
            {"VEKTRA_ASSESS_LLM_MODEL": "t/m"}
        ),
        audit=RecordingAuditSink(),
    )
    bundle = assemble_context(s, _frozen(s))
    with pytest.raises(RagNotAvailableError):
        await r.run(bundle, StudentTurn("ciao", 0))
    with pytest.raises(RagNotAvailableError):
        await r.run_part_b(bundle)
    assert llm.calls == [] and guard.log == [] and guard.post_retrieval_calls == 0

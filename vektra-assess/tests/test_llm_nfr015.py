"""Structural NFR-015 evidence for the discussion runner (plan assess-llm-runner, task D).

FEAT-028: exactly ONE module of ``vektra_assess.llm`` may reach a vector store, the retrieval
function ``llm/retrieval.py`` (exclusion before ranking, its own tests and the canary suite). An
AST scan of every OTHER module finds no name, attribute, import or string that reaches a vector
store; ``post_retrieval`` (the safeguard hook of the platform's RAG, FEAT-026) stays forbidden
everywhere, the retrieval module included. A bundle frozen with course material on a runner that
cannot honour it is refused before anything is rendered; and a run never calls
``post_retrieval`` (the recording safeguard raises if it does).
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
from vektra_testkit.recorders import RecordingLLMProvider

PKG = Path(__file__).resolve().parents[1] / "src" / "vektra_assess" / "llm"
FORBIDDEN = ("VectorStoreProvider", "vector_store", "post_retrieval")
#: the one module allowed to hold the vector store, and what it may still never reference
RETRIEVAL = "retrieval.py"
RETRIEVAL_FORBIDDEN = ("post_retrieval", "raw_filters", "hidden_from_students")


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


def _hits(source: str, forbidden: tuple[str, ...] = FORBIDDEN) -> list[str]:
    refs = _references(ast.parse(source))
    return sorted({f for f in forbidden for r in refs if f in r})


def test_only_the_retrieval_module_references_a_vector_store() -> None:
    files = sorted(PKG.rglob("*.py"))
    assert len(files) >= 10  # the scan really covers the package
    found = {
        str(p.relative_to(PKG)): _hits(p.read_text(encoding="utf-8"))
        for p in files
        if str(p.relative_to(PKG)) != RETRIEVAL
    }
    assert {k: v for k, v in found.items() if v} == {}


def test_the_retrieval_module_uses_the_filter_and_nothing_else() -> None:
    """It reaches the store (the exception is real, not a stale allowlist entry), never through
    ``raw_filters`` or the FEAT-026 visibility key, and never calls ``post_retrieval``."""
    source = (PKG / RETRIEVAL).read_text(encoding="utf-8")
    assert "VectorStoreProvider" in _hits(source)
    assert _hits(source, RETRIEVAL_FORBIDDEN) == []
    # it searches and never writes: Soluzione and pasted rubric are never indexed
    calls = {
        n.func.attr
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "search" in calls
    assert not calls & {"store", "delete", "delete_index_version", "embed_documents"}


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


async def test_a_course_material_bundle_without_rag_is_refused_at_start_only() -> None:
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
    # a Start (Part B) is refused before any call
    with pytest.raises(RagNotAvailableError):
        await r.run_part_b(bundle)
    assert llm.calls == [] and guard.log == [] and guard.post_retrieval_calls == 0
    # a turn of a live session goes on WITHOUT material (review 6): no retrieval of any kind
    await r.run(bundle, StudentTurn("ciao", 0))
    assert b"<<<MATERIALE" not in b"".join(llm.all_request_bytes)
    assert guard.post_retrieval_calls == 0

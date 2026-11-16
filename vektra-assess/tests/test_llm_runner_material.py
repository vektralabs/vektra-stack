"""The runner with course material (FEAT-028, ARCH-072 amendment, REQ-096 (b), NFR-015).

A recording eligibility reader and the real retriever over a filtering store: the eligible
modules of THIS turn are the search filter, their chunks reach the request as one delimited block
with the live titles, ``[Materiale: <titolo>]`` resolves only for those, the trace carries the
chunk ids and cmids, and every way of ending without material makes no search call and is audited.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fakes import RecordingAuditSink, RecordingSafeguard, RecordingTraceWriter
from test_context_bundle import _frozen
from test_context_snapshot import make
from test_llm_retrieval import SITE_KEY, FakeEmbedder, FilteringStore

from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.materials import EligibleModule
from vektra_assess.llm import DiscussionTurnRunner, StudentTurn, TurnResult
from vektra_assess.llm.retrieval import CourseMaterialRetriever
from vektra_assess.llm.types import EligibilityRead
from vektra_testkit.recorders import RecordingLLMProvider

SID, SITE = uuid4(), uuid4()


def _bundle(course_material: bool = True):  # type: ignore[no-untyped-def]
    s = make()
    s = s.model_copy(
        update={
            "header": s.header.model_copy(
                update={
                    "flags": s.header.flags.model_copy(
                        update={"course_material": course_material}
                    )
                }
            )
        }
    )
    return assemble_context(s, _frozen(s))


class Reader:
    def __init__(self, read: EligibilityRead | Exception) -> None:
        self.read = read
        self.calls: list[tuple[Any, Any]] = []

    async def __call__(self, session_id: Any, site_id: Any) -> EligibilityRead:
        self.calls.append((session_id, site_id))
        if isinstance(self.read, Exception):
            raise self.read
        return self.read


def _runner(reader: Reader, store: FilteringStore, reply: str, **kw: Any):  # type: ignore[no-untyped-def]
    llm = RecordingLLMProvider([reply])
    audit, traces = RecordingAuditSink(), RecordingTraceWriter()
    r = DiscussionTurnRunner(
        llm=llm,
        safeguard=RecordingSafeguard(),
        trace_writer=traces,
        settings=VektraAssessSettings.model_validate(
            {
                "VEKTRA_ASSESS_LLM_MODEL": "t/m",
                "VEKTRA_ASSESS_RAG_ENABLED": True,
                **kw,
            }
        ),
        audit=audit,
        retriever=CourseMaterialRetriever(store, FakeEmbedder(), top_k=3),  # type: ignore[arg-type]
        eligibility=reader,
    )
    return r, llm, audit, traces


def _turn(text: str = "Come funziona la fotosintesi?") -> StudentTurn:
    return StudentTurn(text, 1, session_id=SID, site_id=SITE)


READABLE = EligibilityRead(
    True,
    namespace="psico-prova",
    modules=(EligibleModule(1, "Lezione 1"), EligibleModule(2, "Dispensa")),
    lms_site=SITE_KEY,
)
CHUNKS = [(1, "La fotosintesi usa la luce."), (3, "CANARY-HIDDEN"), (2, "Clorofilla.")]


async def test_material_of_eligible_modules_reaches_the_request_and_the_trace() -> None:
    reader, store = Reader(READABLE), FilteringStore(CHUNKS)
    reply = "Lo spiega [Materiale: Lezione 1]. Vedi anche [Materiale: Lezione 9]."
    r, llm, audit, traces = _runner(reader, store, reply)
    result = await r.run(_bundle(), _turn())
    assert reader.calls == [(SID, SITE)]
    assert store.calls[0]["filters"] == {"cmid": [1, 2], "lms_site": SITE_KEY}
    assert store.calls[0]["namespace"] == "psico-prova"
    (sent,) = llm.all_request_bytes
    text = sent.decode("utf-8")
    assert "MATERIALE DEL CORSO" in text and 'titolo=\\"Lezione 1\\"' in text
    assert "La fotosintesi usa la luce." in text and "Clorofilla." in text
    assert "CANARY-HIDDEN" not in text
    # the tag of a module retrieved in this turn stays; any other one is removed and marked
    assert "[Materiale: Lezione 1]" in result.text
    assert "Lezione 9" not in result.text and "citations" in result.marks
    (trace, _ns) = traces.rows[0]
    assert [c.chunk_id for c in trace.chunks_retrieved] == ["c0", "c2"]
    (step,) = [s for s in trace.steps if s.name == "retrieval"]
    assert step.metadata["cmids"] == [1, 2] and step.metadata["searched"] is True
    assert step.metadata["eligible"] == 2
    assert "CANARY" not in str(trace) and "fotosintesi" not in str(step.metadata)
    assert [e for e, _ in audit.rows if e == "assess_material_unavailable"] == []


@pytest.mark.parametrize(
    ("read", "reason"),
    [
        (EligibilityRead(False, reason="modules_unreadable"), "modules_unreadable"),
        (EligibilityRead(False, reason="timeout"), "timeout"),
        # the teacher turned RAG OFF mid-session (review 2): the next turn has no material
        (EligibilityRead(False, reason="rag_withdrawn"), "rag_withdrawn"),
        (EligibilityRead(False, reason="enqueue_failed"), "enqueue_failed"),
        (RuntimeError("boom"), "error"),
        (EligibilityRead(True, namespace=None, modules=()), "unreadable"),
        # no site key: nothing could be filtered on it (FEAT-028 review 1)
        (EligibilityRead(True, namespace="ns", modules=READABLE.modules), "unreadable"),
    ],
)
async def test_an_unreadable_eligibility_means_no_search_call_audited(
    read: Any, reason: str
) -> None:
    reader, store = Reader(read), FilteringStore(CHUNKS)
    r, llm, audit, traces = _runner(reader, store, "Ecco. Vedi [Materiale: Lezione 1].")
    result = await r.run(_bundle(), _turn())
    assert store.calls == []  # NFR-015 amendment: 0 retrieval calls in that turn
    assert (
        "assess_material_unavailable",
        {"turn_index": 1, "reason_code": reason},
    ) in (audit.rows)
    assert "<<<MATERIALE" not in llm.all_request_bytes[0].decode()
    assert "[Materiale" not in result.text  # nothing retrieved: nothing citable
    assert isinstance(result, TurnResult) and result.text  # the turn went on
    assert traces.rows[0][0].chunks_retrieved == []


async def test_no_eligible_module_means_no_search_call_audited() -> None:
    reader = Reader(
        EligibilityRead(True, namespace="ns", modules=(), lms_site=SITE_KEY)
    )
    store = FilteringStore(CHUNKS)
    r, _llm, audit, _ = _runner(reader, store, "Ecco.")
    await r.run(_bundle(), _turn())
    assert store.calls == []
    assert (
        "assess_material_unavailable",
        {"turn_index": 1, "reason_code": "no_eligible_module"},
    ) in audit.rows


async def test_a_failing_store_gives_no_material_and_the_turn_goes_on() -> None:
    class Broken(FilteringStore):
        async def search(self, *a: Any, **k: Any) -> Any:
            raise ConnectionError("down")

    r, _llm, audit, _ = _runner(Reader(READABLE), Broken([]), "Ecco.")
    result = await r.run(_bundle(), _turn())
    assert result.text == "Ecco."
    assert (
        "assess_material_unavailable",
        {"turn_index": 1, "reason_code": "retrieval_error"},
    ) in audit.rows


async def test_a_bundle_without_course_material_reads_nothing() -> None:
    """NFR-015: RAG OFF for the session -> no eligibility read and no retrieval call, even on a
    runner wired for RAG."""
    reader, store = Reader(READABLE), FilteringStore(CHUNKS)
    r, _llm, _audit, traces = _runner(reader, store, "Ecco.")
    await r.run(_bundle(course_material=False), _turn())
    assert reader.calls == [] and store.calls == []
    assert all(s.name != "retrieval" for s in traces.rows[0][0].steps)


async def test_each_turn_reads_the_eligibility_again() -> None:
    """TTL 0: a module that becomes restricted between turns is out of the next search."""
    reader, store = Reader(READABLE), FilteringStore(CHUNKS)
    r, _llm, _a, _t = _runner(reader, store, "Uno.")
    await r.run(_bundle(), _turn())
    reader.read = EligibilityRead(
        True,
        namespace="psico-prova",
        modules=(EligibleModule(2, "Dispensa"),),
        lms_site=SITE_KEY,
    )
    r._llm = RecordingLLMProvider(["Due."])  # type: ignore[assignment]
    await r.run(_bundle(), _turn())
    assert len(reader.calls) == 2
    assert [c["filters"]["cmid"] for c in store.calls] == [[1, 2], [2]]
    assert "La fotosintesi" not in r._llm.all_request_bytes[0].decode()  # type: ignore[attr-defined]


async def test_part_b_retrieves_nothing() -> None:
    reader, store = Reader(READABLE), FilteringStore(CHUNKS)
    r, _llm, _a, _t = _runner(reader, store, "Orientamento breve.")
    try:
        await r.run_part_b(_bundle())
    except Exception:
        pass  # the Part B checks may reject this text; only the absence of calls matters
    assert reader.calls == [] and store.calls == []


def test_the_material_rule_is_in_the_system_prompt_of_a_material_session_only() -> None:
    from vektra_assess.llm.postcheck.lexicon import LEXICON_VERSION
    from vektra_assess.llm.prompts import PromptRenderer

    r = PromptRenderer(LEXICON_VERSION)
    on = " ".join(r.turn_messages(_bundle(), (), "ciao", "it")[0].content.split())
    off = r.turn_messages(_bundle(course_material=False), (), "ciao", "it")[0].content
    assert "MATERIALE DEL CORSO." in on and "[Materiale: <titolo>]" in on
    assert "non cambia la valutazione" in on and "risposta giusta" in on
    assert "MATERIALE DEL CORSO" not in off and "Materiale" not in off

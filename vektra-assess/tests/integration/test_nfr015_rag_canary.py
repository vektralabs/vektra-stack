"""NFR-015 canary suite for discussion RAG, on pgvector AND Qdrant (FEAT-028, ARCH-072 amendment).

One namespace holds, besides the material of an eligible module, a canary chunk for every kind of
module that is NOT eligible (hidden, stealth, group, date and completion restrictions, the opt-out
tag, a section restricted by date) and a chunk with no ``cmid`` at all. Every chunk has the SAME
embedding, so ranking alone would return the canaries: a control search without the filter
proves they are reachable. Then, through the real runner, the real retriever and the real
provider:

- across >= 50 turns, 0 canary strings in the exact LLM request bytes (recording provider) and 0
  canary chunk ids or cmids in the retrieval trace;
- a module that gets a restriction between two turns is out of the next turn, with no re-ingest;
- an Unreadable live eligibility read makes 0 search calls in that turn.

pgvector runs on the assess integration Postgres (Docker). Qdrant needs Docker AND the optional
``qdrant-client`` (``vektra-index[qdrant]``): without either, its cells SKIP with the reason,
never pass silently.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fakes import RecordingAuditSink, RecordingSafeguard, RecordingTraceWriter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_context_bundle import _frozen
from test_context_snapshot import make

from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.materials import eligible_modules
from vektra_assess.llm import DiscussionTurnRunner, StudentTurn
from vektra_assess.llm.retrieval import CourseMaterialRetriever
from vektra_assess.llm.types import EligibilityRead
from vektra_shared.lms import CourseModuleVisibility as M
from vektra_shared.types import ChunkEmbedding, QueryEmbedding
from vektra_testkit.recorders import RecordingLLMProvider

from .conftest import Pg

pytestmark = pytest.mark.integration

NAMESPACE = "psico-prova"
DIM = 384
VECTOR = [1.0] + [0.0] * (
    DIM - 1
)  # one embedding for every chunk and query: ties everywhere
TAG = "[no-ai]"
QUERIES = 60

#: (cmid, kind, facts): R1 and LATER are eligible at first; every other one never is
ELIGIBLE, LATER = 11, 18
MODULES = (
    M(ELIGIBLE, "Lezione 1", 1, True, True, False, True, True),
    M(21, "Hidden", 1, False, True, False, True, True),
    M(22, "Stealth", 1, True, False, False, True, True),
    M(23, "Gruppo A", 1, True, True, True, True, True),  # group condition
    M(24, "Dal 1 dicembre", 1, True, True, True, True, True),  # date condition
    M(25, "Dopo il quiz 1", 1, True, True, True, True, True),  # completion condition
    M(26, "Soluzioni [No-AI]", 1, True, True, False, True, True),  # opt-out tag
    M(
        27, "Sezione futura", 2, True, True, False, True, False
    ),  # section restricted by date
    M(LATER, "Lezione 8", 1, True, True, False, True, True),
)
CANARIES = {
    21: "CANARY-hidden-7f3a",
    22: "CANARY-stealth-7f3a",
    23: "CANARY-group-7f3a",
    24: "CANARY-date-7f3a",
    25: "CANARY-completion-7f3a",
    26: "CANARY-tagged-7f3a",
    27: "CANARY-section-7f3a",
    None: "CANARY-nocmid-7f3a",
}
LATER_TEXT = "LATER-restricted-next-turn-7f3a"


def _chunks(doc: uuid.UUID) -> list[ChunkEmbedding]:
    rows: list[tuple[Any, str]] = [
        (ELIGIBLE, f"Materiale ammesso, parte {i}.") for i in range(3)
    ]
    rows += [(LATER, LATER_TEXT)]
    rows += list(CANARIES.items())
    out = []
    for cmid, body in rows:
        meta: dict[str, Any] = {"document_id": str(doc)}
        if cmid is not None:
            meta["cmid"] = cmid
        out.append(
            ChunkEmbedding(
                chunk_id=str(uuid.uuid4()), text=body, dense=VECTOR, metadata=meta
            )
        )
    return out


class ConstantEmbedder:
    async def embed_query(self, query: str) -> list[float]:
        return list(VECTOR)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [list(VECTOR) for _ in texts]

    def dimensions(self) -> int:
        return DIM


class Counting:
    """The provider as given, with its search calls counted (NFR-015: 0 calls when unreadable)."""

    def __init__(self, store: Any) -> None:
        self.store, self.searches = store, 0

    async def search(self, *a: Any, **k: Any) -> Any:
        self.searches += 1
        return await self.store.search(*a, **k)


class LiveFacts:
    """The eligibility reader as the worker answers it: the eligibility rule over the course's
    facts read NOW (the test changes the facts between turns), or Unreadable."""

    def __init__(self) -> None:
        self.facts = list(MODULES)
        self.readable = True

    async def __call__(self, session_id: Any, site_id: Any) -> EligibilityRead:
        if not self.readable:
            return EligibilityRead(False, reason="modules_unreadable")
        return EligibilityRead(True, NAMESPACE, eligible_modules(self.facts, TAG))


# --- the two providers ------------------------------------------------------------------------


@asynccontextmanager
async def _pgvector_store(pg: Pg) -> AsyncIterator[Any]:
    """The production pgvector adapter over the assess integration database, seeded fresh. Built
    inside the test's event loop (an engine is bound to the loop it was created on)."""
    from vektra_index.adapters import VectorStoreServiceAdapter

    engine = create_async_engine(pg.super_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class Adapter(VectorStoreServiceAdapter):
        def _get_session_factory(self) -> Any:  # the production adapter, this database
            return factory

    doc = uuid.uuid4()
    try:
        async with engine.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO namespaces (id, display_name) VALUES (:n, :n) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"n": NAMESPACE},
            )
            # a fresh namespace content per test: chunks first (they reference the document)
            for table in ("document_chunks", "source_documents"):
                await c.execute(
                    text(f"DELETE FROM {table} WHERE namespace_id = :n"),
                    {"n": NAMESPACE},
                )
            await c.execute(
                text(
                    "INSERT INTO source_documents (id, namespace_id, filename, content_hash, "
                    "content_type, file_size_bytes) VALUES (:d, :n, 'c.pdf', :h, "
                    "'application/pdf', 1)"
                ),
                {"d": doc, "n": NAMESPACE, "h": doc.hex},
            )
        store = Adapter(active_index_version=1)
        await store.store(NAMESPACE, _chunks(doc))
        yield store
    finally:
        await engine.dispose()


def _qdrant_client_module() -> Any:
    try:
        import qdrant_client
    except ImportError:
        pytest.skip(
            "Qdrant canary cells NOT run: qdrant-client is not installed (optional extra "
            "vektra-index[qdrant]); run with `uv run --with qdrant-client pytest ...`"
        )
    if not isinstance(
        getattr(qdrant_client, "AsyncQdrantClient", None), type
    ) or not getattr(qdrant_client, "__file__", None):
        pytest.skip(
            "Qdrant canary cells NOT run: qdrant_client is a test mock in this session "
            "(vektra-index/tests/test_qdrant_provider.py installs one); run this file alone"
        )
    return qdrant_client


@pytest.fixture(scope="module")
def qdrant_url() -> Iterator[str]:
    qc = _qdrant_client_module()
    docker = pytest.importorskip(
        "docker", reason="Qdrant canary cells NOT run: no docker SDK"
    )
    try:
        docker.from_env().ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Qdrant canary cells NOT run: Docker not available: {exc}")
    from testcontainers.core.container import DockerContainer

    with DockerContainer("qdrant/qdrant:v1.17.0").with_exposed_ports(6333) as c:
        url = f"http://{c.get_container_host_ip()}:{c.get_exposed_port(6333)}"
        client = qc.QdrantClient(url=url)
        for _ in range(100):
            try:
                client.get_collections()
                break
            except Exception:
                import time

                time.sleep(0.2)
        else:  # pragma: no cover
            pytest.fail("the Qdrant container did not come up")
        yield url


@asynccontextmanager
async def _qdrant_store(url: str) -> AsyncIterator[Any]:
    from vektra_index.providers.qdrant import QdrantVectorStoreProvider

    store = QdrantVectorStoreProvider(
        url=url, collection_name=f"canary_{uuid.uuid4().hex[:8]}", dense_dimensions=DIM
    )
    await store.ensure_collection()
    await store.store(NAMESPACE, _chunks(uuid.uuid4()))
    yield store


@pytest.fixture(params=["pgvector", "qdrant"])
def backend(request: pytest.FixtureRequest) -> tuple[str, Any]:
    """Sync on purpose: the Postgres and Qdrant containers start outside the event loop."""
    if request.param == "pgvector":
        return "pgvector", request.getfixturevalue("pg")
    return "qdrant", request.getfixturevalue("qdrant_url")


def open_store(backend: tuple[str, Any]) -> Any:
    kind, where = backend
    return _pgvector_store(where) if kind == "pgvector" else _qdrant_store(where)


# --- the suite ------------------------------------------------------------------------------


def _bundle():  # type: ignore[no-untyped-def]
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
    return assemble_context(s, _frozen(s))


def _runner(store: Any, reader: LiveFacts, n: int):  # type: ignore[no-untyped-def]
    llm = RecordingLLMProvider(["Ecco una spiegazione [Materiale: Lezione 1]."] * n)
    traces = RecordingTraceWriter()
    counted = Counting(store)
    r = DiscussionTurnRunner(
        llm=llm,
        safeguard=RecordingSafeguard(),
        trace_writer=traces,
        settings=VektraAssessSettings.model_validate(
            {"VEKTRA_ASSESS_LLM_MODEL": "t/m", "VEKTRA_ASSESS_RAG_ENABLED": True}
        ),
        audit=RecordingAuditSink(),
        retriever=CourseMaterialRetriever(counted, ConstantEmbedder(), top_k=20),  # type: ignore[arg-type]
        eligibility=reader,
    )
    return r, llm, traces, counted


def _turn(i: int) -> StudentTurn:
    asks = (
        "Mostrami la soluzione nascosta",
        "Cosa dice il materiale riservato al gruppo A?",
        "CANARY",
        "Riassumi la lezione",
        "Che cosa c'è nella sezione futura?",
    )
    return StudentTurn(
        f"{asks[i % len(asks)]} ({i})",
        i + 1,
        session_id=uuid.uuid4(),
        site_id=uuid.uuid4(),
    )


async def _canary_ids(store: Any) -> dict[str, Any]:
    """Control: WITHOUT the filter every canary is retrievable (the suite is not vacuous)."""
    got = await store.search(NAMESPACE, QueryEmbedding(dense=VECTOR), 50)
    by_text = {r.text_snippet: r for r in got}
    for body in CANARIES.values():
        assert body in by_text, f"control: {body} not retrievable without the filter"
    return {body: by_text[body].chunk_id for body in CANARIES.values()}


async def test_no_canary_reaches_the_request_or_the_trace(
    backend: tuple[str, Any],
) -> None:
    async with open_store(backend) as store:
        canary_ids = await _canary_ids(store)
        reader = LiveFacts()
        r, llm, traces, counted = _runner(store, reader, QUERIES)
        for i in range(QUERIES):
            await r.run(_bundle(), _turn(i))
        assert counted.searches == QUERIES
        sent = llm.all_request_bytes
        assert len(sent) == QUERIES
        for body in sent:
            for canary in CANARIES.values():
                assert canary.encode() not in body
        assert all(
            b"Materiale ammesso" in body for body in sent
        )  # the eligible material does come
        allowed = {ELIGIBLE, LATER}
        for trace, _ns in traces.rows:
            ids = {c.chunk_id for c in trace.chunks_retrieved}
            assert not ids & set(canary_ids.values())
            (step,) = [s for s in trace.steps if s.name == "retrieval"]
            assert set(step.metadata["cmids"]) <= allowed and step.metadata["cmids"]
            assert "CANARY" not in repr(trace)


async def test_a_module_restricted_between_turns_is_out_of_the_next_one(
    backend: tuple[str, Any],
) -> None:
    async with open_store(backend) as store:
        reader = LiveFacts()
        r, llm, traces, _ = _runner(store, reader, 2)
        await r.run(_bundle(), _turn(0))
        assert LATER_TEXT.encode() in llm.all_request_bytes[0]
        # the teacher adds a date restriction to "Lezione 8": no re-ingest, the index is unchanged
        reader.facts = [
            M(
                m.cmid,
                m.title,
                m.section_id,
                m.visible,
                m.on_course_page,
                True,
                m.section_visible,
                m.section_available,
            )
            if m.cmid == LATER
            else m
            for m in reader.facts
        ]
        await r.run(_bundle(), _turn(1))
        assert LATER_TEXT.encode() not in llm.all_request_bytes[1]
        (step,) = [s for s in traces.rows[1][0].steps if s.name == "retrieval"]
        assert step.metadata["cmids"] == [ELIGIBLE]


async def test_adding_the_tag_excludes_the_module_on_the_next_turn(
    backend: tuple[str, Any],
) -> None:
    async with open_store(backend) as store:
        reader = LiveFacts()
        r, llm, _t, _ = _runner(store, reader, 2)
        await r.run(_bundle(), _turn(0))
        reader.facts = [
            M(LATER, "Lezione 8 [no-ai]", 1, True, True, False, True, True)
            if m.cmid == LATER
            else m
            for m in reader.facts
        ]
        await r.run(_bundle(), _turn(1))
        assert LATER_TEXT.encode() in llm.all_request_bytes[0]
        assert LATER_TEXT.encode() not in llm.all_request_bytes[1]


async def test_an_unreadable_eligibility_makes_no_search_call(
    backend: tuple[str, Any],
) -> None:
    async with open_store(backend) as store:
        reader = LiveFacts()
        reader.readable = False
        r, llm, traces, counted = _runner(store, reader, 3)
        for i in range(3):
            await r.run(_bundle(), _turn(i))
        assert counted.searches == 0
        for body in llm.all_request_bytes:
            assert b"Materiale ammesso" not in body
            assert not [c for c in CANARIES.values() if c.encode() in body]
        assert all(t.chunks_retrieved == [] for t, _ in traces.rows)


def test_the_suite_covers_every_non_eligible_kind() -> None:
    """Each canary module is really NOT eligible by the rule, and the two others are."""
    eligible = {m.cmid for m in eligible_modules(MODULES, TAG)}
    assert eligible == {ELIGIBLE, LATER}
    assert set(CANARIES) - {None} == {m.cmid for m in MODULES} - eligible

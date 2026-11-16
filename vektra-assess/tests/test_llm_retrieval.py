"""The course-material retrieval function (FEAT-028, ARCH-072, NFR-015).

A recording store that applies the filter as both providers do (IN on ``metadata.cmid``) shows
what the function sends: the eligible cmids as the only filter, never ``raw_filters``, no call
at all without an eligible module, and titles from the eligibility read.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from vektra_assess.context.materials import EligibleModule, course_namespace
from vektra_assess.llm.retrieval import (
    FILTER_VIOLATION,
    NO_ELIGIBLE_MODULE,
    CourseMaterialRetriever,
    cmid_of,
)
from vektra_shared.types import QueryEmbedding, SearchMode, SearchResult


class FakeEmbedder:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return [0.1, 0.2]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("retrieval never embeds documents")

    def dimensions(self) -> int:
        return 2


class FilteringStore:
    """Holds (cmid-or-None, text) chunks and filters like the real providers: pre-ranking IN."""

    def __init__(self, chunks: list[tuple[Any, str]], *, ignore_filter: bool = False):
        self.chunks = chunks
        self.calls: list[dict[str, Any]] = []
        self.ignore_filter = ignore_filter

    async def search(
        self,
        namespace: str,
        query_embedding: QueryEmbedding,
        top_k: int,
        search_mode: SearchMode = SearchMode.DENSE,
        filters: Any = None,
        raw_filters: Any = None,
    ) -> list[SearchResult]:
        self.calls.append(
            {
                "namespace": namespace,
                "top_k": top_k,
                "mode": search_mode,
                "filters": filters,
                "raw_filters": raw_filters,
            }
        )
        allowed = None if self.ignore_filter else set((filters or {}).get("cmid", []))
        out = []
        for i, (cmid, text) in enumerate(self.chunks):
            meta: dict[str, Any] = {} if cmid is None else {"cmid": cmid}
            if allowed is not None and cmid_of(meta) not in allowed:
                continue
            out.append(
                SearchResult(f"c{i}", 1.0 - i / 100, text, uuid4(), metadata=meta)
            )
        return out[:top_k]


MODULES = (EligibleModule(1, "Lezione 1"), EligibleModule(2, "Lezione 2"))


async def test_the_filter_is_the_eligible_cmids_and_nothing_else() -> None:
    store = FilteringStore([(1, "a"), (3, "secret"), (None, "unbound"), (2, "b")])
    emb = FakeEmbedder()
    r = CourseMaterialRetriever(store, emb, top_k=4)  # type: ignore[arg-type]
    got = await r.retrieve("psico-prova", "domanda", MODULES)
    assert store.calls == [
        {
            "namespace": "psico-prova",
            "top_k": 4,
            "mode": SearchMode.DENSE,
            "filters": {"cmid": [1, 2]},
            "raw_filters": None,
        }
    ]
    assert emb.queries == ["domanda"]
    assert got.searched and got.reason is None
    assert [(c.cmid, c.title, c.text) for c in got.chunks] == [
        (1, "Lezione 1", "a"),
        (2, "Lezione 2", "b"),
    ]
    assert got.titles == {"Lezione 1", "Lezione 2"} and got.cmids == (1, 2)


async def test_no_eligible_module_means_no_call_at_all() -> None:
    store, emb = FilteringStore([(1, "a")]), FakeEmbedder()
    r = CourseMaterialRetriever(store, emb, top_k=4)  # type: ignore[arg-type]
    got = await r.retrieve("ns", "q", ())
    assert store.calls == [] and emb.queries == []
    assert not got.searched and got.reason == NO_ELIGIBLE_MODULE and got.chunks == ()


async def test_a_store_that_ignores_the_filter_gives_no_material() -> None:
    """Fail closed, never a post-filter: one chunk outside the set voids the whole search."""
    store = FilteringStore([(1, "a"), (3, "secret")], ignore_filter=True)
    r = CourseMaterialRetriever(store, FakeEmbedder(), top_k=4)  # type: ignore[arg-type]
    got = await r.retrieve("ns", "q", MODULES)
    assert got.searched and got.reason == FILTER_VIOLATION and got.chunks == ()


async def test_an_unbound_chunk_returned_by_the_store_voids_the_search() -> None:
    store = FilteringStore([(1, "a"), (None, "unbound")], ignore_filter=True)
    r = CourseMaterialRetriever(store, FakeEmbedder(), top_k=4)  # type: ignore[arg-type]
    assert (await r.retrieve("ns", "q", MODULES)).reason == FILTER_VIOLATION


async def test_titles_come_from_the_eligibility_read_not_the_chunk() -> None:
    store = FilteringStore([("2", "b")])  # a string binding, as a payload may hold it
    r = CourseMaterialRetriever(store, FakeEmbedder(), top_k=1)  # type: ignore[arg-type]
    got = await r.retrieve("ns", "q", (EligibleModule(2, "Titolo vivo"),))
    assert [c.title for c in got.chunks] == ["Titolo vivo"]


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"cmid": 5}, 5),
        ({"cmid": "5"}, 5),
        ({"cmid": True}, None),
        ({"cmid": "5a"}, None),
        ({"cmid": "\u0665"}, None),  # a non-ASCII digit is no binding
        ({"cmid": 5.0}, None),
        ({}, None),
    ],
)
def test_cmid_binding(meta: dict[str, Any], expected: int | None) -> None:
    assert cmid_of(meta) == expected


def test_top_k_must_be_positive() -> None:
    with pytest.raises(ValueError):
        CourseMaterialRetriever(FilteringStore([]), FakeEmbedder(), top_k=0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("shortname", "namespace"),
    [
        ("psicologia-generale", "psicologia-generale"),
        ("Course 101", "course-101"),
        ("Física Cuántica", "fisica-cuantica"),
        ("Diritto: intro", "diritto-intro"),
        ("PSICO-PROVA", "psico-prova"),
        ("--a__b--", "a__b"),
        ("x" * 60, "x" * 50),
        ("???", "course-9"),
        (None, "course-9"),
    ],
)
def test_course_namespace_matches_the_ingest_convention(
    shortname: str | None, namespace: str
) -> None:
    assert course_namespace(shortname, 9) == namespace

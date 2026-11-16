"""Course-material retrieval for one discussion turn (FEAT-028, ARCH-072, NFR-015).

The ONE module of ``vektra_assess.llm`` that reaches a vector store (``test_llm_nfr015`` allows
it and nothing else). It holds the platform's ``VectorStoreProvider`` and ``EmbeddingProvider``
Protocols, injected by the composition root in vektra_app; it imports no implementation.

The exclusion happens BEFORE ranking, inside the search itself: the turn's eligible cmids go to
``search`` as the metadata filter ``{"cmid": [...]}`` (a list is IN on both providers: pgvector's
``->>`` comparison, Qdrant's ``MatchAny`` pre-filter), so a chunk of a module that is not eligible
never enters the candidate set, and a chunk WITHOUT ``cmid`` can never match. The raw
provider-filter escape hatch is never used. With no eligible module there is no search call.

A result whose ``cmid`` is not in the turn's set would mean the provider ignored the filter: the
whole turn's material is then discarded (fail closed), never filtered after the fact.

Titles come from the live eligibility read (the module's title in the LMS now), never from chunk
metadata. The activity's Soluzione and pasted rubric are not in the index at all: nothing in
vektra_assess writes to a vector store (this module only calls ``search``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from vektra_assess.context.materials import EligibleModule
from vektra_shared.protocols import EmbeddingProvider, VectorStoreProvider
from vektra_shared.types import QueryEmbedding, SearchFilters, SearchMode

#: the metadata key the ingest pipeline binds every course document to (FEAT-028)
CMID_KEY = "cmid"
#: and the site it comes from (``vektra_shared.site_key.lms_site_key``): a cmid is unique per site
SITE_KEY = "lms_site"

#: why a turn has no material: closed codes, safe to audit and to trace
NO_ELIGIBLE_MODULE = "no_eligible_module"
FILTER_VIOLATION = "filter_violation"


@dataclass(frozen=True)
class MaterialChunk:
    """One retrieved chunk as the prompt shows it: the module's live title and the text."""

    chunk_id: str
    cmid: int
    title: str
    text: str
    score: float


@dataclass(frozen=True)
class Retrieval:
    """The material of one turn. ``searched``: whether a search call was made at all."""

    chunks: tuple[MaterialChunk, ...]
    searched: bool
    reason: str | None = None  # a closed code when there is no material by rule

    @property
    def titles(self) -> frozenset[str]:
        return frozenset(c.title for c in self.chunks)

    @property
    def cmids(self) -> tuple[int, ...]:
        return tuple(sorted({c.cmid for c in self.chunks}))


#: what a title may not carry in the prompt: the block's own fences and quote, and the square
#: brackets of a citation tag (a ``]`` inside would end ``[Materiale: ...]`` early)
_TITLE_REPLACE = (
    ("<<<", "< < <"),
    (">>>", "> > >"),
    ('"', "'"),
    ("[", "("),
    ("]", ")"),
)


def display_title(title: str) -> str:
    """A module title as the prompt shows it AND as ``[Materiale: <titolo>]`` must cite it: one
    rewrite for both, so the tag the model copies from the block resolves (FEAT-028)."""
    for old, new in _TITLE_REPLACE:
        title = title.replace(old, new)
    return " ".join(title.split())


def cmid_of(metadata: Mapping[str, Any]) -> int | None:
    """The chunk's cmid binding: a JSON integer, or a string of ASCII digits (the providers
    return what the payload holds). Anything else, a bool included, is no binding."""
    value = metadata.get(CMID_KEY)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


class CourseMaterialRetriever:
    def __init__(
        self,
        store: VectorStoreProvider,
        embedder: EmbeddingProvider,
        *,
        top_k: int,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        self._store = store
        self._embedder = embedder
        self._top_k = top_k

    @property
    def top_k(self) -> int:
        return self._top_k

    async def retrieve(
        self,
        namespace: str,
        query: str,
        eligible: Sequence[EligibleModule],
        lms_site: str,
    ) -> Retrieval:
        """The chunks of ``namespace`` closest to ``query`` among the ``eligible`` modules of
        THIS site (``lms_site``) only: a chunk of another site, or of none, never matches.

        No eligible module -> no embedding and no search call (``NO_ELIGIBLE_MODULE``)."""
        titles = {int(m.cmid): m.title for m in eligible}
        if not titles:
            return Retrieval((), searched=False, reason=NO_ELIGIBLE_MODULE)
        allowed = sorted(titles)
        dense = await self._embedder.embed_query(query)
        results = await self._store.search(
            namespace,
            QueryEmbedding(dense=dense),
            self._top_k,
            SearchMode.DENSE,
            # IN, before ranking (NFR-015); SearchFilters accepts any key at runtime
            filters=cast(SearchFilters, {CMID_KEY: allowed, SITE_KEY: lms_site}),
        )
        chunks: list[MaterialChunk] = []
        for r in results:
            cmid = cmid_of(r.metadata)
            if (
                cmid is None
                or cmid not in titles
                or r.metadata.get(SITE_KEY) != lms_site
            ):
                # the store returned a chunk outside the filter: nothing of this search is used
                return Retrieval((), searched=True, reason=FILTER_VIOLATION)
            chunks.append(
                MaterialChunk(
                    r.chunk_id,
                    cmid,
                    display_title(titles[cmid]),
                    r.text_snippet,
                    r.score,
                )
            )
        return Retrieval(tuple(chunks), searched=True)

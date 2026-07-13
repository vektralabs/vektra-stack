"""TEIEmbeddingProvider: EmbeddingProvider over HuggingFace Text Embeddings Inference.

Remote embedding via a TEI instance (FEAT-024, ADR-0013). Lets deployments
reuse a shared inference service (e.g. bge-m3 on the host) instead of
duplicating in-process CPU embedding per container, and unlocks models
whose sequence window exceeds the in-process default (bge-m3: 8192 tokens
vs 128 for paraphrase-multilingual-MiniLM-L12-v2).

Endpoints used (TEI native API):
- POST /embed  {"inputs": [...]} -> [[...], ...]
- GET  /info   for the embedding size (with an /embed probe fallback)

Auth: optional Bearer token (TEI --api-key).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from vektra_shared.types import HealthStatus

logger = logging.getLogger(__name__)

# TEI rejects batches larger than its --max-client-batch-size (default 32).
_MAX_BATCH = 32


class TEIEmbeddingProvider:
    """EmbeddingProvider backed by a remote TEI instance.

    Symmetric encoding: TEI applies the model's own pooling/normalization;
    query and document paths use the same endpoint.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        _client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = _client or httpx.AsyncClient(
            base_url=self._url, headers=headers, timeout=timeout_s
        )
        self._dimensions: int | None = None

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.post("/embed", json={"inputs": texts})
        resp.raise_for_status()
        data: list[list[float]] = resp.json()
        if data and self._dimensions is None:
            # Warm the dimensions cache so the startup warmup (embed_query)
            # makes the later synchronous dimensions() call free.
            self._dimensions = len(data[0])
        return data

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of document passages, chunked to the TEI batch limit."""
        out: list[list[float]] = []
        for i in range(0, len(texts), _MAX_BATCH):
            out.extend(await self._embed_batch(texts[i : i + _MAX_BATCH]))
        return out

    async def embed_query(self, query: str) -> list[float]:
        """Embed a single query string for retrieval."""
        return (await self._embed_batch([query]))[0]

    def dimensions(self) -> int:
        """Return the embedding dimensionality, fetched once from the server.

        The Protocol method is synchronous, so this uses a one-off sync HTTP
        call (startup/wiring path, not the query hot path). Tries /info
        first; TEI versions that do not expose the size fall back to probing
        /embed with a single input.
        """
        if self._dimensions is not None:
            return self._dimensions

        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        with httpx.Client(
            base_url=self._url, headers=headers, timeout=self._timeout_s
        ) as client:
            self._dimensions = self._fetch_dimensions(client)

        logger.info("TEI embedding provider: %s (%d dims)", self._url, self._dimensions)
        return self._dimensions

    @staticmethod
    def _fetch_dimensions(client: httpx.Client) -> int:
        try:
            resp = client.get("/info")
            resp.raise_for_status()
            info: Any = resp.json()
            if isinstance(info, dict):
                for key in ("embedding_size", "hidden_size"):
                    if isinstance(info.get(key), int):
                        return int(info[key])
        except (httpx.HTTPError, ValueError):
            logger.debug("TEI /info unavailable or invalid, probing /embed")
        resp = client.post("/embed", json={"inputs": ["dim probe"]})
        resp.raise_for_status()
        return len(resp.json()[0])

    async def health_check(self) -> HealthStatus:
        """Verify the TEI server responds and can produce an embedding."""
        try:
            start = time.monotonic()
            await self.embed_query("health check")
            latency_ms = int((time.monotonic() - start) * 1000)
            return HealthStatus(status="healthy", latency_ms=latency_ms)
        except Exception as exc:
            return HealthStatus(status="unhealthy", message=str(exc))

    async def aclose(self) -> None:
        """Release the underlying HTTP client (tests and shutdown)."""
        await self._client.aclose()

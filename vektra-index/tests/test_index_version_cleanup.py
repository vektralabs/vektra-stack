"""Cleanup of a superseded index version (REQ-064, DEBT-032).

Reindex writes a second copy of every chunk under the target version and leaves
both in the store. Nothing used to remove the loser, so every reindex doubled a
namespace's storage forever and the only exit was a hand-written delete against
the store.

The destructive failure mode of that hand-written delete is not "an old version
survives", it is "the live index is emptied": one wrong version number and the
chunks that are answering queries are gone. So the guard these tests exist for
is the refusal to delete the active version, and they check the refusal at the
level where it is enforced (the store, which is the only thing that knows what
it is serving) as well as through the endpoint.

The endpoint tests wire a real provider with a stubbed client rather than an
AsyncMock store: a mocked guard proves nothing about the guard.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from vektra_shared.auth import ApiKeyInfo
from vektra_shared.errors import ActiveIndexVersionError
from vektra_shared.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Fake qdrant_client
# ---------------------------------------------------------------------------
#
# qdrant-client is not installed for unit tests (CI runs `uv sync --dev` with no
# extras), so the module has to be stubbed. test_qdrant_provider.py stubs it with
# a bare MagicMock, which cannot answer "what filter did we build?": every
# attribute is another MagicMock. Half of what matters here is precisely the
# filter -- a delete that pins the version but forgets the namespace wipes that
# version for every tenant -- so these stand-ins keep their arguments readable.
#
# The provider imports qdrant_client lazily inside each method, so what counts is
# what sits in sys.modules when the method *runs*, not when this file is imported.
# pytest imports test_qdrant_provider.py after this one and its bare MagicMock
# would otherwise be the module in force by then. Hence an autouse fixture, which
# installs the fake per test and restores the previous entry afterwards.


class _MatchValue:
    def __init__(self, value: Any) -> None:
        self.value = value


class _FieldCondition:
    def __init__(self, key: str, match: Any) -> None:
        self.key = key
        self.match = match


class _Filter:
    def __init__(
        self, must: list[Any] | None = None, must_not: list[Any] | None = None
    ) -> None:
        self.must = must or []
        self.must_not = must_not or []


class _FilterSelector:
    def __init__(self, filter: Any) -> None:
        self.filter = filter


@pytest.fixture(autouse=True)
def fake_qdrant(monkeypatch: pytest.MonkeyPatch) -> None:
    models = ModuleType("qdrant_client.models")
    models.Filter = _Filter  # type: ignore[attr-defined]
    models.FieldCondition = _FieldCondition  # type: ignore[attr-defined]
    models.MatchValue = _MatchValue  # type: ignore[attr-defined]
    models.FilterSelector = _FilterSelector  # type: ignore[attr-defined]

    qdrant = ModuleType("qdrant_client")
    qdrant.models = models  # type: ignore[attr-defined]
    qdrant.AsyncQdrantClient = MagicMock  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "qdrant_client", qdrant)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", models)


def _selector_conditions(client: MagicMock) -> dict[str, Any]:
    """The namespace/version pair the delete filter actually pinned."""
    selector = client.delete.await_args.kwargs["points_selector"]
    return {c.key: c.match.value for c in selector.filter.must}


# ---------------------------------------------------------------------------
# Qdrant provider
# ---------------------------------------------------------------------------


def _make_qdrant_client(count: int = 7) -> MagicMock:
    client = MagicMock()
    client.count = AsyncMock(return_value=MagicMock(count=count))
    client.delete = AsyncMock()
    return client


class TestQdrantDeleteIndexVersion:
    @pytest.mark.asyncio
    async def test_deletes_a_superseded_version_and_reports_the_count(self) -> None:
        from vektra_index.providers.qdrant import QdrantVectorStoreProvider

        client = _make_qdrant_client(count=7)
        provider = QdrantVectorStoreProvider(active_index_version=2, _client=client)

        removed = await provider.delete_index_version("default", 1)

        assert removed == 7
        client.delete.assert_awaited_once()

        # The filter must pin both namespace and version: pinning only the
        # version would delete that version across every tenant.
        assert _selector_conditions(client) == {
            "namespace_id": "default",
            "index_version": 1,
        }

    @pytest.mark.asyncio
    async def test_refuses_the_active_version_and_deletes_nothing(self) -> None:
        """The whole point of DEBT-032: this call would empty the live index."""
        from vektra_index.providers.qdrant import QdrantVectorStoreProvider

        client = _make_qdrant_client()
        provider = QdrantVectorStoreProvider(active_index_version=1, _client=client)

        with pytest.raises(ActiveIndexVersionError):
            await provider.delete_index_version("default", 1)

        client.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_second_cleanup_reports_nothing_left(self) -> None:
        """Idempotent: chunks_removed=0 is how the operator confirms it is gone."""
        from vektra_index.providers.qdrant import QdrantVectorStoreProvider

        client = _make_qdrant_client(count=0)
        provider = QdrantVectorStoreProvider(active_index_version=2, _client=client)

        assert await provider.delete_index_version("default", 1) == 0


# ---------------------------------------------------------------------------
# Pgvector provider
# ---------------------------------------------------------------------------


class TestPgvectorDeleteIndexVersion:
    @staticmethod
    def _make_session(count: int = 5) -> AsyncMock:
        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=MagicMock(**{"scalar_one.return_value": count})
        )
        session.flush = AsyncMock()
        return session

    @pytest.mark.asyncio
    async def test_deletes_a_superseded_version_and_reports_the_count(self) -> None:
        from vektra_index.providers.pgvector import PgvectorProvider

        session = self._make_session(count=5)
        provider = PgvectorProvider(active_index_version=2)

        removed = await provider.delete_index_version(session, "default", 1)

        assert removed == 5
        # COUNT then DELETE.
        assert session.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_refuses_the_active_version_and_deletes_nothing(self) -> None:
        from vektra_index.providers.pgvector import PgvectorProvider

        session = self._make_session()
        provider = PgvectorProvider(active_index_version=1)

        with pytest.raises(ActiveIndexVersionError):
            await provider.delete_index_version(session, "default", 1)

        session.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# DELETE /api/v1/index-versions/{index_version}
# ---------------------------------------------------------------------------


def _make_app(
    active_index_version: int,
    client: MagicMock,
    namespace_id: str | None = None,
) -> FastAPI:
    """App wired with a real Qdrant provider over a stubbed client.

    The guard under test is the provider's, so the provider has to be real.
    """
    from vektra_index.providers.qdrant import QdrantVectorStoreProvider
    from vektra_index.reindex import router

    key_store = AsyncMock()
    key_store.lookup_by_token = AsyncMock(
        return_value=ApiKeyInfo(
            key_id=uuid4(), scopes=["admin"], namespace_id=namespace_id
        )
    )

    app = FastAPI()
    app.state.registry = ProviderRegistry()
    app.state.registry.register("key_store", "default", key_store)
    app.state.registry.register(
        "vector_store",
        "default",
        QdrantVectorStoreProvider(
            active_index_version=active_index_version, _client=client
        ),
    )
    app.include_router(router)
    return app


async def _delete_version(
    app: FastAPI, version: int, params: dict | None = None
) -> tuple[int, dict]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        resp = await http.delete(
            f"/api/v1/index-versions/{version}",
            params=params or {},
            headers={"Authorization": "Bearer test-token"},
        )
    return resp.status_code, resp.json()


@pytest.mark.asyncio
async def test_endpoint_removes_the_old_version() -> None:
    client = _make_qdrant_client(count=12)
    app = _make_app(active_index_version=2, client=client)

    status, body = await _delete_version(app, 1, {"namespace": "default"})

    assert status == 200
    assert body["chunks_removed"] == 12
    assert body["index_version"] == 1
    assert body["namespace"] == "default"
    client.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_endpoint_refuses_to_delete_the_version_being_served() -> None:
    """The criterion that matters: this request must not empty the live index.

    Version 1 is active, so deleting it is the operator error that takes the
    system down. It has to fail loudly, and it has to fail *before* the store
    is touched.
    """
    client = _make_qdrant_client(count=12)
    app = _make_app(active_index_version=1, client=client)

    status, body = await _delete_version(app, 1, {"namespace": "default"})

    assert status == 409
    assert "currently being served" in body["detail"]
    client.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_rejects_version_zero() -> None:
    client = _make_qdrant_client()
    app = _make_app(active_index_version=1, client=client)

    status, _ = await _delete_version(app, 0)

    assert status == 400
    client.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_refuses_a_namespace_outside_the_key_binding() -> None:
    """A bound key must not clean up another tenant's version (H5).

    This deletes by filter, so a silently retargeted namespace would drop a
    whole version of a namespace the caller never named.
    """
    client = _make_qdrant_client()
    app = _make_app(active_index_version=2, client=client, namespace_id="tenant-a")

    status, _ = await _delete_version(app, 1, {"namespace": "tenant-b"})

    assert status == 403
    client.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_uses_the_key_namespace_when_none_is_given() -> None:
    client = _make_qdrant_client(count=3)
    app = _make_app(active_index_version=2, client=client, namespace_id="tenant-a")

    status, body = await _delete_version(app, 1)

    assert status == 200
    assert body["namespace"] == "tenant-a"
    assert _selector_conditions(client)["namespace_id"] == "tenant-a"

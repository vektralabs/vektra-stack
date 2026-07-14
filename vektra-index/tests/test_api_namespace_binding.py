"""Namespace binding on the chunk endpoints (H5).

A namespace-bound API key must not be able to reach another namespace, and must
not need to name its own namespace to reach it.

`DELETE /documents/{id}` never enforced the binding: it took the namespace
straight from the query string. That was survivable only while the delete was a
no-op against the active store (BUG-023); now that it actually removes the
chunks, an admin key bound to one namespace could delete another tenant's
documents. Found in review of the BUG-023 PR.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from vektra_shared.auth import ApiKeyInfo
from vektra_shared.db import get_session
from vektra_shared.registry import ProviderRegistry


def _make_key_store(scopes: list[str], namespace_id: str | None) -> AsyncMock:
    store = AsyncMock()
    store.lookup_by_token = AsyncMock(
        return_value=ApiKeyInfo(
            key_id=uuid4(), scopes=scopes, namespace_id=namespace_id
        )
    )
    return store


def _make_session() -> AsyncMock:
    """Session stub: delete_document soft-deletes the row inside session.begin()."""
    session = AsyncMock()
    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=session)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)
    session.execute = AsyncMock()
    return session


def _make_app(vector_store: AsyncMock, namespace_id: str | None) -> FastAPI:
    from vektra_index.api import router

    app = FastAPI()
    app.state.registry = ProviderRegistry()
    app.state.registry.register(
        "key_store", "default", _make_key_store(["admin"], namespace_id)
    )
    app.state.registry.register("vector_store", "default", vector_store)
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: _make_session()
    return app


async def _delete(app: FastAPI, doc_id: str, params: dict) -> int:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete(
            f"/api/v1/documents/{doc_id}",
            params=params,
            headers={"Authorization": "Bearer test-token"},
        )
    return resp.status_code


async def _list_chunks(app: FastAPI, doc_id: str, params: dict) -> int:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/v1/documents/{doc_id}/chunks",
            params=params,
            headers={"Authorization": "Bearer test-token"},
        )
    return resp.status_code


@pytest.mark.asyncio
async def test_delete_rejects_a_namespace_outside_the_key_binding() -> None:
    """The cross-namespace deletion this endpoint used to allow."""
    vector_store = AsyncMock()
    vector_store.delete = AsyncMock(return_value=0)
    app = _make_app(vector_store, namespace_id="tenant-a")

    status = await _delete(app, str(uuid4()), {"namespace": "tenant-b"})

    assert status == 403
    vector_store.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_without_a_namespace_uses_the_key_binding() -> None:
    """A bound key must not have to name its own namespace to delete in it."""
    vector_store = AsyncMock()
    vector_store.delete = AsyncMock(return_value=3)
    app = _make_app(vector_store, namespace_id="tenant-a")
    doc_id = str(uuid4())

    status = await _delete(app, doc_id, {})

    assert status == 200
    vector_store.delete.assert_awaited_once_with("tenant-a", [doc_id])


@pytest.mark.asyncio
async def test_list_chunks_without_a_namespace_uses_the_key_binding() -> None:
    """Defaulting the query param to "default" would 403 a bound key that omits it."""
    vector_store = AsyncMock()
    vector_store.list_chunks = AsyncMock(return_value=[])
    app = _make_app(vector_store, namespace_id="tenant-a")
    doc_id = str(uuid4())

    status = await _list_chunks(app, doc_id, {})

    assert status == 200
    assert vector_store.list_chunks.await_args.args[0] == "tenant-a"


@pytest.mark.asyncio
async def test_list_chunks_rejects_a_namespace_outside_the_key_binding() -> None:
    vector_store = AsyncMock()
    vector_store.list_chunks = AsyncMock(return_value=[])
    app = _make_app(vector_store, namespace_id="tenant-a")

    status = await _list_chunks(app, str(uuid4()), {"namespace": "tenant-b"})

    assert status == 403
    vector_store.list_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbound_key_still_reaches_any_namespace() -> None:
    """An unscoped admin key keeps working across namespaces."""
    vector_store = AsyncMock()
    vector_store.delete = AsyncMock(return_value=1)
    app = _make_app(vector_store, namespace_id=None)
    doc_id = str(uuid4())

    status = await _delete(app, doc_id, {"namespace": "anything"})

    assert status == 200
    vector_store.delete.assert_awaited_once_with("anything", [doc_id])

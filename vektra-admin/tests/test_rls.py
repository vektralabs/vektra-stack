"""Unit tests for RLS namespace isolation middleware (ADR-0009, ARCH-025)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_admin.rls import RLSMiddleware, _resolve_namespace, set_rls_namespace


def _make_request(
    *,
    method: str = "GET",
    query_params: dict[str, str] | None = None,
    body: bytes | None = None,
    multi_tenant: bool = True,
) -> MagicMock:
    """Build a mock Starlette Request."""
    request = MagicMock()
    request.method = method
    request.query_params = query_params or {}
    request.state = MagicMock(spec=[])  # no pre-set attributes on state
    request.app.state.multi_tenant = multi_tenant

    async def _body() -> bytes:
        return body or b""

    request.body = _body
    return request


class TestResolveNamespace:
    @pytest.mark.asyncio
    async def test_from_query_param(self):
        """Namespace resolved from query parameter."""
        request = _make_request(query_params={"namespace": "ns-123"})
        result = await _resolve_namespace(request)
        assert result == "ns-123"

    @pytest.mark.asyncio
    async def test_from_json_body(self):
        """Namespace resolved from POST JSON body."""
        import json

        body = json.dumps({"namespace": "ns-456"}).encode()
        request = _make_request(method="POST", body=body)
        result = await _resolve_namespace(request)
        assert result == "ns-456"

    @pytest.mark.asyncio
    async def test_body_takes_precedence_over_query_param(self):
        """For write methods, body namespace takes precedence over query param."""
        import json

        body = json.dumps({"namespace": "body-ns"}).encode()
        request = _make_request(
            method="POST",
            query_params={"namespace": "query-ns"},
            body=body,
        )
        result = await _resolve_namespace(request)
        assert result == "body-ns"

    @pytest.mark.asyncio
    async def test_get_uses_query_param_over_body(self):
        """For GET requests, query param is used (body not read)."""
        request = _make_request(
            method="GET",
            query_params={"namespace": "query-ns"},
        )
        result = await _resolve_namespace(request)
        assert result == "query-ns"

    @pytest.mark.asyncio
    async def test_post_without_body_falls_back_to_query_param(self):
        """POST without body namespace falls back to query param."""
        import json

        body = json.dumps({"other_field": "value"}).encode()
        request = _make_request(
            method="POST",
            query_params={"namespace": "fallback-ns"},
            body=body,
        )
        result = await _resolve_namespace(request)
        assert result == "fallback-ns"

    @pytest.mark.asyncio
    async def test_no_namespace_returns_none(self):
        """No namespace in request returns None."""
        request = _make_request(method="GET")
        result = await _resolve_namespace(request)
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        """Invalid JSON body returns None."""
        request = _make_request(method="POST", body=b"not json")
        result = await _resolve_namespace(request)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_body_returns_none(self):
        """Empty body returns None."""
        request = _make_request(method="POST", body=b"")
        result = await _resolve_namespace(request)
        assert result is None

    @pytest.mark.asyncio
    async def test_non_string_namespace_returns_none(self):
        """Non-string namespace value in JSON returns None."""
        import json

        body = json.dumps({"namespace": 123}).encode()
        request = _make_request(method="POST", body=body)
        result = await _resolve_namespace(request)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_request_ignores_body(self):
        """GET request does not read body."""
        import json

        body = json.dumps({"namespace": "ns-from-body"}).encode()
        request = _make_request(method="GET", body=body)
        result = await _resolve_namespace(request)
        assert result is None


class TestRLSMiddleware:
    @pytest.mark.asyncio
    async def test_single_tenant_passthrough(self):
        """Multi-tenant disabled: middleware is a no-op."""
        call_next = AsyncMock(return_value=MagicMock())
        request = _make_request(multi_tenant=False)

        app = MagicMock()
        middleware = RLSMiddleware(app)
        await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)
        assert not hasattr(request.state, "rls_namespace")

    @pytest.mark.asyncio
    async def test_multi_tenant_sets_rls_namespace(self):
        """Multi-tenant enabled: namespace set on request.state."""
        call_next = AsyncMock(return_value=MagicMock())
        request = _make_request(
            multi_tenant=True,
            query_params={"namespace": "ns-rls"},
        )

        app = MagicMock()
        middleware = RLSMiddleware(app)
        await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)
        assert request.state.rls_namespace == "ns-rls"

    @pytest.mark.asyncio
    async def test_multi_tenant_no_namespace(self):
        """Multi-tenant enabled but no namespace: no rls_namespace set."""
        call_next = AsyncMock(return_value=MagicMock())
        request = _make_request(multi_tenant=True)

        app = MagicMock()
        middleware = RLSMiddleware(app)
        await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)
        assert not hasattr(request.state, "rls_namespace")


class TestSetRlsNamespace:
    @pytest.mark.asyncio
    async def test_executes_set_local(self):
        """set_rls_namespace executes SET LOCAL on AsyncSession."""
        session = AsyncMock(spec=AsyncSession)
        await set_rls_namespace(session, "ns-test")
        session.execute.assert_called_once()
        call_args = session.execute.call_args
        # First positional arg is the text() SQL
        sql_text = str(call_args[0][0])
        assert "SET LOCAL" in sql_text
        assert "app.current_namespace" in sql_text
        assert call_args[0][1] == {"ns": "ns-test"}

    @pytest.mark.asyncio
    async def test_non_async_session_raises_type_error(self):
        """Non-AsyncSession raises TypeError (fail-closed)."""
        session = MagicMock()  # not an AsyncSession
        with pytest.raises(TypeError, match="requires AsyncSession"):
            await set_rls_namespace(session, "ns-test")

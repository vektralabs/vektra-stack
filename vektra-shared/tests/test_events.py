"""Unit tests for WebhookEventEmitter (ARCH-038, Phase 2)."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from vektra_shared.config import WebhookConfig
from vektra_shared.events import NoOpEventEmitter, WebhookEventEmitter
from vektra_shared.protocols import EventEmitter


def _make_config(**overrides) -> WebhookConfig:
    defaults = {
        "VEKTRA_WEBHOOK_URL": "https://example.com/hook",
        "VEKTRA_WEBHOOK_SECRET": "test-secret",
        "VEKTRA_WEBHOOK_TIMEOUT": 5.0,
    }
    defaults.update(overrides)
    return WebhookConfig(**defaults)


class TestSign:
    def test_sign_produces_correct_hmac_sha256(self) -> None:
        config = _make_config()
        emitter = WebhookEventEmitter(config)
        body = b'{"event_type":"test","payload":{}}'

        result = emitter._sign(body, "test-secret")

        expected = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        assert result == expected

    def test_sign_different_secrets_produce_different_digests(self) -> None:
        body = b"same-body"
        e1 = WebhookEventEmitter(_make_config(VEKTRA_WEBHOOK_SECRET="secret-a"))
        e2 = WebhookEventEmitter(_make_config(VEKTRA_WEBHOOK_SECRET="secret-b"))
        assert e1._sign(body, "secret-a") != e2._sign(body, "secret-b")


class TestEmit:
    @pytest.mark.asyncio
    async def test_emit_sends_post_with_correct_headers(self) -> None:
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200)

        config = _make_config()
        emitter = WebhookEventEmitter(config)
        emitter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        await emitter.emit("document.indexed", {"doc_id": "abc"})

        assert len(captured) == 1
        req = captured[0]
        assert req.method == "POST"
        assert req.headers["content-type"] == "application/json"
        assert req.headers["x-vektra-signature-256"].startswith("sha256=")

    @pytest.mark.asyncio
    async def test_emit_body_contains_event_type_and_timestamp(self) -> None:
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200)

        config = _make_config()
        emitter = WebhookEventEmitter(config)
        emitter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        await emitter.emit("query.completed", {"response_id": "xyz"})

        body = json.loads(captured[0].content)
        assert body["event_type"] == "query.completed"
        assert "timestamp" in body
        assert body["payload"] == {"response_id": "xyz"}

    @pytest.mark.asyncio
    async def test_emit_signature_matches_body(self) -> None:
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200)

        config = _make_config()
        emitter = WebhookEventEmitter(config)
        emitter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        await emitter.emit("test.event", {})

        req = captured[0]
        body_bytes = req.content
        expected_sig = hmac.new(b"test-secret", body_bytes, hashlib.sha256).hexdigest()
        assert req.headers["x-vektra-signature-256"] == f"sha256={expected_sig}"

    @pytest.mark.asyncio
    async def test_emit_no_signature_header_when_secret_is_none(self) -> None:
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200)

        config = _make_config(VEKTRA_WEBHOOK_SECRET=None)
        emitter = WebhookEventEmitter(config)
        emitter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        await emitter.emit("test.event", {})

        req = captured[0]
        assert "x-vektra-signature-256" not in req.headers
        assert req.headers["content-type"] == "application/json"

    @pytest.mark.asyncio
    async def test_emit_does_not_raise_on_http_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        config = _make_config()
        emitter = WebhookEventEmitter(config)
        emitter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        # Should not raise
        await emitter.emit("document.failed", {"error": "timeout"})

    @pytest.mark.asyncio
    async def test_emit_does_not_raise_on_connection_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        config = _make_config()
        emitter = WebhookEventEmitter(config)
        emitter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        # Should not raise
        await emitter.emit("apikey.created", {"key_id": "123"})

    @pytest.mark.asyncio
    async def test_emit_skips_when_url_is_none(self) -> None:
        config = WebhookConfig(
            VEKTRA_WEBHOOK_URL=None,
            VEKTRA_WEBHOOK_SECRET="s",
        )
        emitter = WebhookEventEmitter(config)
        assert emitter._client is None
        # Should return immediately without error (no HTTP client created)
        await emitter.emit("test.event", {})

    @pytest.mark.asyncio
    async def test_aclose_closes_client(self) -> None:
        config = _make_config()
        emitter = WebhookEventEmitter(config)
        assert emitter._client is not None
        await emitter.aclose()

    @pytest.mark.asyncio
    async def test_aclose_noop_when_no_client(self) -> None:
        config = WebhookConfig(VEKTRA_WEBHOOK_URL=None)
        emitter = WebhookEventEmitter(config)
        assert emitter._client is None
        await emitter.aclose()  # Should not raise


class TestProtocolCompliance:
    def test_webhook_emitter_implements_event_emitter_protocol(self) -> None:
        config = _make_config()
        emitter = WebhookEventEmitter(config)
        assert isinstance(emitter, EventEmitter)

    def test_noop_emitter_implements_event_emitter_protocol(self) -> None:
        emitter = NoOpEventEmitter()
        assert isinstance(emitter, EventEmitter)

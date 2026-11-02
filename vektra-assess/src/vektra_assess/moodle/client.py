"""Hardened Moodle web-service client (ARCH-070, plan assess-moodle-ws group B).

``MoodleWsClient.call(site, wsfunction, params)`` returns a ``Readable(json)`` or an
``Unreadable(reason)``; it never raises for a Moodle or network failure.

- ``follow_redirects=False``: a 3xx is ``Unreadable(redirect)`` and the Location is never
  followed (one request, asserted in a test);
- the connection is pinned to the validated IP (``vektra_shared.netguard``) and TLS is
  verified against the original hostname (SNI), with ``VEKTRA_ASSESS_MOODLE_CA_FILE`` or the
  system store; plain http only with ``VEKTRA_ENV=development`` and ``..._ALLOW_HTTP=true``;
- the token travels in the POST body (``wstoken``), never in a URL that could be logged;
- the response is streamed with a byte cap and aborted on overflow;
- the per-site breaker and the global token bucket are consulted before any traffic;
- logging carries wsfunction, site_id, http status, duration_ms, bytes and the Moodle
  errorcode. Never a body, a parameter, a URL or the token.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

from vektra_assess.config import VektraAssessSettings
from vektra_assess.moodle.breaker import BreakerRegistry
from vektra_assess.moodle.errors import (
    error_code,
    is_error_envelope,
    unreadable_from_envelope,
)
from vektra_assess.moodle.ratelimit import AsyncTokenBucket
from vektra_assess.moodle.result import Readable, Reading, Unreadable, UnreadableReason
from vektra_assess.moodle.shape import evidence_for
from vektra_shared.netguard import EgressDenied, Resolver, resolve_pinned

log = structlog.get_logger()

REST_PATH = "/webservice/rest/server.php"
_UNSET = object()


@dataclass(frozen=True)
class SiteRef:
    site_id: str
    base_url: str  # https://moodle.example.org  (no path, no credentials)
    release: str | None = None


def flatten_params(params: Mapping[str, Any]) -> dict[str, str]:
    """Moodle REST encoding: ``{"ids": [1, 2]}`` -> ``ids[0]=1&ids[1]=2``."""
    out: dict[str, str] = {}

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, Mapping):
            for k, v in value.items():
                walk(f"{prefix}[{k}]", v)
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                walk(f"{prefix}[{i}]", v)
        elif isinstance(value, bool):
            out[prefix] = "1" if value else "0"
        else:
            out[prefix] = str(value)

    for key, value in params.items():
        walk(key, value)
    return out


def _is_tls_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class MoodleWsClient:
    def __init__(
        self,
        settings: VektraAssessSettings,
        *,
        token_provider: Callable[[str], str | None],
        breakers: BreakerRegistry | None = None,
        limiter: AsyncTokenBucket | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = socket.getaddrinfo,
    ) -> None:
        self._s = settings
        self._token_for = token_provider
        self._breakers = breakers or BreakerRegistry(
            settings.breaker_threshold, settings.breaker_cooldown_s
        )
        self._limiter = limiter or AsyncTokenBucket(settings.ws_rps)
        self._resolver = resolver
        verify: ssl.SSLContext | bool = True
        if settings.moodle_ca_file is not None:
            verify = ssl.create_default_context(cafile=str(settings.moodle_ca_file))
        self._http = httpx.AsyncClient(
            transport=transport,
            verify=verify,
            follow_redirects=False,
            timeout=httpx.Timeout(
                connect=settings.ws_timeout_connect_s,
                read=settings.ws_timeout_read_s,
                write=settings.ws_timeout_read_s,
                pool=settings.ws_timeout_connect_s,
            ),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _allow_http(self) -> bool:
        return self._s.moodle_allow_http and self._s.env == "development"

    def _cidrs(self) -> list[str]:
        return [c.strip() for c in self._s.moodle_allowed_cidrs.split(",") if c.strip()]

    async def call(
        self, site: SiteRef, wsfunction: str, params: Mapping[str, Any] | None = None
    ) -> Reading[Any]:
        started = time.perf_counter()
        status: int | None = None
        size = 0
        code = ""
        result: Reading[Any]
        breaker = self._breakers.for_site(site.site_id)
        if not breaker.allow():
            result = Unreadable(UnreadableReason.BREAKER_OPEN)
        else:
            result, status, size, code = await self._exchange(
                site, wsfunction, params or {}
            )
        log.info(
            "moodle_ws_call",
            wsfunction=wsfunction,
            site_id=site.site_id,
            status=status,
            duration_ms=int((time.perf_counter() - started) * 1000),
            bytes=size,
            errorcode=code or None,
            outcome="ok" if isinstance(result, Readable) else result.reason.value,
        )
        return result

    async def _exchange(
        self, site: SiteRef, wsfunction: str, params: Mapping[str, Any]
    ) -> tuple[Reading[Any], int | None, int, str]:
        breaker = self._breakers.for_site(site.site_id)
        token = self._token_for(site.site_id)
        if not token:
            breaker.record_success()  # nothing was sent: do not hold the probe slot
            return (
                Unreadable(UnreadableReason.ACCESS_EXCEPTION, "no_token"),
                None,
                0,
                "",
            )
        parts = urlsplit(site.base_url)
        if parts.scheme == "http" and not self._allow_http():
            breaker.record_success()
            return Unreadable(UnreadableReason.TLS, "plain_http_refused"), None, 0, ""
        try:
            target = resolve_pinned(
                site.base_url.rstrip("/") + REST_PATH,
                allowed_hosts=[parts.hostname or ""],
                allowed_cidrs=self._cidrs(),
                resolver=self._resolver,
            )
        except EgressDenied:
            breaker.record_success()
            return (
                Unreadable(UnreadableReason.ACCESS_EXCEPTION, "egress_denied"),
                None,
                0,
                "",
            )

        form = {
            "wstoken": token,
            "wsfunction": wsfunction,
            "moodlewsrestformat": "json",
            **flatten_params(params),
        }
        await self._limiter.acquire()
        try:
            async with self._http.stream(
                "POST",
                target.pinned_url,
                data=form,
                headers={"Host": target.host_header},
                extensions={"sni_hostname": target.sni_hostname},
            ) as resp:
                status = resp.status_code
                if 300 <= status < 400:
                    breaker.record_success()
                    return (
                        Unreadable(UnreadableReason.REDIRECT, f"http_{status}"),
                        status,
                        0,
                        "",
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > self._s.ws_max_response_bytes:
                        breaker.record_success()
                        return Unreadable(UnreadableReason.SIZE_CAP), status, size, ""
                    chunks.append(chunk)
        except httpx.TimeoutException:
            breaker.record_failure()
            return Unreadable(UnreadableReason.TIMEOUT), None, 0, ""
        except httpx.TransportError as exc:
            if _is_tls_error(exc):
                breaker.record_success()  # the host answered; this is a trust problem
                return (
                    Unreadable(UnreadableReason.TLS, "tls_verify_failed"),
                    None,
                    0,
                    "",
                )
            breaker.record_failure()
            return (
                Unreadable(UnreadableReason.UNKNOWN_SHAPE, "transport_error"),
                None,
                0,
                "",
            )

        if status >= 500:
            breaker.record_failure()
            return (
                Unreadable(UnreadableReason.UNKNOWN_SHAPE, "http_5xx"),
                status,
                size,
                "",
            )
        breaker.record_success()
        if status != 200:
            return (
                Unreadable(UnreadableReason.UNKNOWN_SHAPE, f"http_{status}"),
                status,
                size,
                "",
            )
        try:
            doc = json.loads(b"".join(chunks))
        except (ValueError, RecursionError):
            return (
                Unreadable(UnreadableReason.UNKNOWN_SHAPE, "not_json"),
                status,
                size,
                "",
            )
        if is_error_envelope(doc):
            return unreadable_from_envelope(doc), status, size, error_code(doc)
        return (
            Readable(doc, evidence_for(wsfunction, site.release, doc)),
            status,
            size,
            "",
        )

"""Numbers read from headers and token claims are ASCII digits only (11a review follow-up).

``str.isdigit`` also accepts superscripts ("²", which ``int`` rejects: a 500 instead of a 400) and
other scripts' decimal digits ("١٢", which ``int`` silently reads as 12). One test per site.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from fastapi import FastAPI, Request

from vektra_assess.api.common import read_capped_body
from vektra_assess.api.deps import parse_version
from vektra_assess.api.download_tokens import DownloadTokens
from vektra_assess.api.events import _last_event_id
from vektra_assess.config import VektraAssessSettings
from vektra_assess.keys import AssessKeyring
from vektra_assess.tokens import AssessTokens, TokenError, keyring_from
from vektra_shared.http_errors import register_error_handlers

NOT_ASCII = ["²", "١٢", "\uff11\uff12", "12²"]
#: as a header carries them: the UTF-8 bytes (Starlette decodes headers as Latin-1) and the
#: raw Latin-1 superscript b"\xb2", which "isdigit" accepted and "int" rejected (a 500)
NOT_ASCII_HEADERS = [value.encode("utf-8") for value in NOT_ASCII] + [b"\xb2"]


def _ring() -> AssessKeyring:
    return AssessKeyring("assess_token_signing", "k1", {"k1": os.urandom(32)})


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/body")
    async def body(request: Request) -> dict[str, int]:
        return {"n": len(await read_capped_body(request, max_bytes=8))}

    @app.get("/last")
    async def last(request: Request) -> dict[str, int]:
        return {"id": _last_event_id(request)}

    return app


@pytest.mark.parametrize("declared", NOT_ASCII_HEADERS)
async def test_a_non_ascii_content_length_is_not_a_500(declared: bytes) -> None:
    """``api/common.py``: the declared length is compared only when it is ASCII digits; the
    streamed cap still applies (here the body fits, so it is read)."""
    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/body",
            "headers": [(b"content-length", declared)],
        }
    )

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"abc", "more_body": False}

    req._receive = receive  # type: ignore[attr-defined]
    assert await read_capped_body(req, max_bytes=8) == b"abc"


async def test_a_body_exactly_at_the_cap_is_read() -> None:
    """CodeRabbit nitpick (PR #179): Content-Length 8 with 8 bytes against an 8-byte cap is
    read whole (the limit is inclusive)."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://t"
    ) as c:
        r = await c.post("/body", content=b"x" * 8, headers={"Content-Length": "8"})
    assert r.status_code == 200, r.text
    assert r.json() == {"n": 8}


async def test_an_ascii_content_length_over_the_cap_is_still_refused() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://t"
    ) as c:
        r = await c.post("/body", content=b"x" * 9)
    assert r.status_code == 400
    assert r.json()["error"]["details"]["reason"] == "body_too_large"


async def test_an_oversized_ascii_content_length_is_refused_before_reading() -> None:
    """CodeRabbit nitpick (PR #179 round 3): the declared length alone refuses; the body is
    never read (a receive that fails if called)."""
    from fastapi import HTTPException

    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/body",
            "headers": [(b"content-length", b"9")],
        }
    )

    async def receive() -> dict[str, object]:
        raise AssertionError("the body must not be read")

    req._receive = receive  # type: ignore[attr-defined]
    with pytest.raises(HTTPException) as exc:
        await read_capped_body(req, max_bytes=8)
    assert exc.value.detail["error"]["details"]["reason"] == "body_too_large"


@pytest.mark.parametrize(
    "declared", [b"1" * 5000, b"0" * 5000 + b"9"], ids=["5000_digits", "zero_padded_9"]
)
async def test_a_very_long_ascii_content_length_is_refused_not_a_500(
    declared: bytes,
) -> None:
    """CodeRabbit 4512009230 (PR #179): a declared length longer than Python's int-conversion
    limit (4300 digits) is still ``body_too_large``, never a ValueError; leading zeros do not
    make a small value look huge."""
    from fastapi import HTTPException

    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/body",
            "headers": [(b"content-length", declared)],
        }
    )

    async def receive() -> dict[str, object]:
        raise AssertionError("the body must not be read")

    req._receive = receive  # type: ignore[attr-defined]
    with pytest.raises(HTTPException) as exc:
        await read_capped_body(req, max_bytes=8)
    assert exc.value.detail["error"]["details"]["reason"] == "body_too_large"


@pytest.mark.parametrize("declared", NOT_ASCII_HEADERS)
async def test_a_non_ascii_content_length_does_not_bypass_the_streamed_cap(
    declared: bytes,
) -> None:
    """CodeRabbit nitpick (PR #179 round 2): an unparsable declared length is ignored, and
    the cap applied while the body streams still refuses an oversized body."""
    from fastapi import HTTPException

    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/body",
            "headers": [(b"content-length", declared)],
        }
    )
    chunks = [b"x" * 5, b"x" * 5]

    async def receive() -> dict[str, object]:
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    req._receive = receive  # type: ignore[attr-defined]
    with pytest.raises(HTTPException) as exc:
        await read_capped_body(req, max_bytes=8)
    assert exc.value.status_code == 400
    assert exc.value.detail["error"]["details"]["reason"] == "body_too_large"


@pytest.mark.parametrize("raw", NOT_ASCII_HEADERS)
def test_a_non_ascii_last_event_id_replays_from_zero(raw: bytes) -> None:
    """``api/events.py``: an unparsable Last-Event-ID is 0 (full replay), never a 500."""
    req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/last",
            "headers": [(b"last-event-id", raw)],
        }
    )
    assert _last_event_id(req) == 0


def test_an_ascii_last_event_id_is_read() -> None:
    req = Request(
        {"type": "http", "method": "GET", "headers": [(b"last-event-id", b"42")]}
    )
    assert _last_event_id(req) == 42


@pytest.mark.parametrize("raw", ["1.²", "1.١٢.0", "\uff11.2"])
def test_a_non_ascii_plugin_version_is_unparsable(raw: str) -> None:
    """``api/deps.py``: refused (fails closed), never reinterpreted, never a 500."""
    assert parse_version(raw) is None


def test_an_ascii_plugin_version_parses() -> None:
    assert parse_version("1.12.3") == (1, 12, 3, 0)


@pytest.mark.parametrize("sub", NOT_ASCII)
def test_a_download_token_with_a_non_ascii_sub_is_refused(sub: str) -> None:
    """``api/download_tokens.py``: a signed token whose ``sub`` is "١٢" is not user 12."""
    t = DownloadTokens(keyring_from(_ring()), issuer="vektra", ttl_s=60)
    m = t.mint(sub=sub, tid=uuid.uuid4(), site=uuid.uuid4(), role="owner")  # type: ignore[arg-type]
    with pytest.raises(TokenError):
        t.verify(m.token)
    ok = t.mint(sub=12, tid=uuid.uuid4(), site=uuid.uuid4(), role="owner")
    assert t.verify(ok.token).sub == 12


@pytest.mark.parametrize("sub", NOT_ASCII)
def test_a_discussion_token_with_a_non_ascii_sub_is_refused(sub: str) -> None:
    """``tokens.py`` (same pattern): ``claim_sub``, never a 500, never user 12."""
    t = AssessTokens.from_settings(_ring(), VektraAssessSettings())
    m = t._mint({"sub": sub, "site": str(uuid.uuid4()), "cmid": 7, "stage": "pre"}, 60)
    with pytest.raises(TokenError) as exc:
        t.verify(m.token, stage="pre")
    assert exc.value.code == "claim_sub"

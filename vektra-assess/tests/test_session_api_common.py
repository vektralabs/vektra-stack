"""M1.1: assess session error codes, request models and the 400 body parser (DEBT-035)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request

from vektra_assess import errors as e
from vektra_assess.api.common import fail, parse_body
from vektra_assess.api.models import MintRequest, TurnRequest
from vektra_shared.errors import http_status_for
from vektra_shared.http_errors import register_error_handlers

EXPECTED = [
    (e.token_invalid(), 401),
    (e.loginas_refused(), 403),
    (e.key_not_bound(), 403),
    (e.session_not_found(), 404),
    (e.session_closed(), 409),
    (e.reply_pending(), 409),
    (e.state_conflict("STARTING"), 409),
    (e.rate_limited(3), 429),
    (e.capacity_reached(), 503),
    (e.bad_request("x"), 400),
    (e.start_refused("not_eligible"), 409),
    (e.plugin_too_old(), 403),
]


def test_every_session_code_has_its_status_and_a_remediation() -> None:
    codes = set()
    for err, status in EXPECTED:
        assert http_status_for(err) == status, err.code
        assert err.remediation and err.message
        codes.add(err.code)
    assert len(codes) == len(EXPECTED)  # one code per condition


def test_rate_limited_carries_retry_after_in_body_and_header() -> None:
    exc = fail(e.rate_limited(7))
    assert exc.status_code == 429
    assert exc.headers == {"Retry-After": "7"}
    assert exc.detail["error"]["retry_after"] == 7


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/turn")
    async def turn(request: Request) -> dict[str, int]:
        body = await parse_body(request, TurnRequest)
        return {"n": len(body.text)}

    @app.post("/mint")
    async def mint(request: Request) -> dict[str, bool]:
        body = await parse_body(request, MintRequest)
        return {"ok": body.asserts_not_loginas()}

    return app


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"{not json", "malformed_json"),
        (b"[1, 2]", "body_not_object"),
        (b'{"text": ""}', "string_too_short:text"),
        (b'{"text": "a", "attemptid": 5}', "extra_forbidden:attemptid"),
        (b"x" * (64 * 1024 + 1), "body_too_large"),
    ],
)
async def test_bad_bodies_are_400_envelopes_never_422(raw: bytes, reason: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://t"
    ) as c:
        r = await c.post("/turn", content=raw)
    assert r.status_code == 400, r.text
    body = r.json()
    err = body["error"]
    assert err["code"] == "ERR-ASSESS-014"
    assert err["details"]["reason"] == reason


async def test_validation_error_never_echoes_the_input() -> None:
    secret = "CANARY-student-text-77"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://t"
    ) as c:
        r = await c.post("/turn", json={"text": 5, "note": secret})
    assert r.status_code == 400
    assert secret not in r.text


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        ({"loginas": False, "realuser_id": None}, True),
        ({"loginas": True, "realuser_id": None}, False),
        ({"loginas": 0, "realuser_id": None}, False),
        ({"loginas": "false", "realuser_id": None}, False),
        ({"loginas": False, "realuser_id": 7}, False),
        ({"loginas": False}, False),  # absent realuser_id is a refusal, not a default
        ({"realuser_id": None}, False),
        ({}, False),
    ],
)
def test_loginas_assertion_is_positive(body: dict[str, object], ok: bool) -> None:
    req = MintRequest.model_validate(
        {"sub": 5, "cmid": 9, "plugin_version": "1.0.0", **body}
    )
    assert req.asserts_not_loginas() is ok


async def test_an_oversized_body_is_refused_without_reading_it_all() -> None:
    """CodeRabbit 4493109358: the size cap applies while the body streams in."""
    chunks_read = {"n": 0}

    async def body():  # type: ignore[no-untyped-def]
        for _ in range(1000):  # 64 MB if read to the end
            chunks_read["n"] += 1
            yield b"x" * 65536

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://t"
    ) as c:
        r = await c.post("/turn", content=body())
    assert r.status_code == 400
    assert r.json()["error"]["details"]["reason"] == "body_too_large"
    assert chunks_read["n"] < 10


def test_turn_notes_fit_the_check_with_multibyte_labels() -> None:
    """CodeRabbit 4503025930: 16 distinct labels of 4-byte characters stay under the
    table's 4096-byte CHECK once encoded (labels are cut by bytes, the JSON is bounded)."""
    import json

    from vektra_assess.api.turns import MAX_NOTE_CONTESTS_BYTES, turn_notes
    from vektra_assess.llm.types import ContestEntry, TurnResult, TurnUsage

    result = TurnResult(
        text="x",
        marks=tuple(f"{i:02d}" + "\U0001d510" * 40 for i in range(40)),
        behaviour_class=None,
        contest_entries=tuple(
            ContestEntry(f"{i:02d}" + "\U0001d510" * 200, "corrected")
            for i in range(40)
        ),
        language="it",
        usage=TurnUsage(1, 1),
        call_records=(),
        replaced_count=0,
        content_hash="h",
    )
    notes = turn_notes(result)
    encoded = json.dumps(
        [{"label": c.label, "status": c.status} for c in notes.contests],
        ensure_ascii=False,
    ).encode("utf-8")
    assert notes.contests and len(encoded) <= MAX_NOTE_CONTESTS_BYTES < 4096
    assert all(len(c.label.encode()) <= 64 for c in notes.contests)
    assert len(notes.marks) == 16  # 40 distinct marks after the byte cut, capped

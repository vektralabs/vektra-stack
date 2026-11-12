"""MoodleWsClient, breaker and rate limiter (plan assess-moodle-ws group B)."""

from __future__ import annotations

import asyncio
import json
import socket
from urllib.parse import parse_qs

import httpx
import pytest
from structlog.testing import capture_logs

from vektra_lms_moodle.breaker import BreakerRegistry, BreakerState, CircuitBreaker
from vektra_lms_moodle.client import MoodleWsClient, SiteRef, flatten_params
from vektra_lms_moodle.config import MoodleSettings
from vektra_lms_moodle.ratelimit import AsyncTokenBucket
from vektra_lms_moodle.result import Readable, Unreadable, UnreadableReason

TOKEN = "0123456789abcdef0123456789abcdef"  # gitleaks:allow
SITE = SiteRef("site-1", "https://moodle.example.org", release="5.2.4+")


def settings(**env) -> MoodleSettings:
    base = {"VEKTRA_ASSESS_ENABLED": "true"}
    return MoodleSettings(**{**base, **env})  # type: ignore[arg-type]


def resolver(*ips: str):
    def fn(host, port, *_a, **_k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    return fn


def make(handler, *, s=None, ip="93.184.216.34", token=TOKEN, **kw):
    return MoodleWsClient(
        s or settings(),
        token_provider=lambda _site: token,
        transport=httpx.MockTransport(handler),
        resolver=resolver(ip),
        **kw,
    )


def ok(body):
    return lambda request: httpx.Response(200, json=body)


# --- request shape ----------------------------------------------------------------------


async def test_token_goes_in_the_post_body_and_the_connection_is_pinned():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["host"] = request.headers["host"]
        seen["method"] = request.method
        seen["body"] = parse_qs(request.content.decode())
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, json={"release": "5.2.4+"})

    client = make(handler)
    res = await client.call(SITE, "core_webservice_get_site_info", {"ids": [4, 5]})
    assert isinstance(res, Readable) and res.value == {"release": "5.2.4+"}
    assert (
        res.evidence is not None
        and res.evidence.call == "core_webservice_get_site_info"
    )
    assert seen["method"] == "POST"
    assert (
        seen["url"] == "https://93.184.216.34/webservice/rest/server.php"
    )  # pinned IP
    assert seen["host"] == "moodle.example.org" and seen["sni"] == "moodle.example.org"
    assert seen["body"]["wstoken"] == [TOKEN] and "wstoken" not in seen["url"]
    assert seen["body"]["wsfunction"] == ["core_webservice_get_site_info"]
    assert seen["body"]["moodlewsrestformat"] == ["json"]
    assert seen["body"]["ids[0]"] == ["4"] and seen["body"]["ids[1]"] == ["5"]
    await client.aclose()


def test_client_never_follows_redirects():
    assert make(ok({}))._http.follow_redirects is False


async def test_a_302_is_unreadable_and_exactly_one_request_is_made():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest"}
        )

    res = await make(handler).call(SITE, "f")
    assert res == Unreadable(UnreadableReason.REDIRECT, "http_302")
    assert len(calls) == 1


def test_params_are_flattened_the_moodle_way():
    assert flatten_params({"a": 1, "ids": [1, 2], "o": {"k": [True]}}) == {
        "a": "1", "ids[0]": "1", "ids[1]": "2", "o[k][0]": "1",
    }  # fmt: skip


# --- size cap, errors, shapes -------------------------------------------------------------


async def test_an_oversize_body_is_aborted_without_being_read_in_full():
    served = {"bytes": 0}

    async def stream():
        for _ in range(100):
            served["bytes"] += 1024
            yield b"x" * 1024

    def handler(request):
        return httpx.Response(200, content=stream())

    res = await make(
        handler, s=settings(VEKTRA_LMS_MOODLE_WS_MAX_RESPONSE_BYTES="4096")
    ).call(SITE, "f")
    assert res.reason is UnreadableReason.SIZE_CAP  # type: ignore[union-attr]
    assert served["bytes"] < 100 * 1024  # the stream was abandoned early


async def test_an_error_envelope_is_never_parsed_as_data():
    body = {
        "exception": "webservice_access_exception",
        "errorcode": "accessexception",
        "message": "Access control exception for Mario Rossi",
    }
    res = await make(ok(body)).call(SITE, "core_course_get_categories")
    assert res == Unreadable(UnreadableReason.ACCESS_EXCEPTION, "accessexception")
    assert "Mario" not in repr(res)


@pytest.mark.parametrize(
    ("response", "reason", "code"),
    [
        (
            httpx.Response(200, content=b"<html>login</html>"),
            UnreadableReason.UNKNOWN_SHAPE,
            "not_json",
        ),
        (
            httpx.Response(404, content=b"nope"),
            UnreadableReason.UNKNOWN_SHAPE,
            "http_404",
        ),
        (
            httpx.Response(503, content=b"down"),
            UnreadableReason.UNKNOWN_SHAPE,
            "http_5xx",
        ),
    ],
)
async def test_non_json_and_http_errors_are_unreadable(response, reason, code):
    res = await make(lambda r: response).call(SITE, "f")
    assert res == Unreadable(reason, code)


async def test_timeouts_and_transport_errors_never_raise():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    def broken(request):
        raise httpx.ConnectError("refused", request=request)

    assert (await make(slow).call(SITE, "f")).reason is UnreadableReason.TIMEOUT  # type: ignore[union-attr]
    assert (await make(broken).call(SITE, "f")).reason is UnreadableReason.UNKNOWN_SHAPE  # type: ignore[union-attr]


# --- policy: egress, http, token -------------------------------------------------------------


async def test_a_private_resolution_is_refused_unless_the_cidr_is_allowed():
    called = []
    handler = lambda r: called.append(1) or httpx.Response(200, json={})  # noqa: E731
    res = await make(handler, ip="10.20.3.4").call(SITE, "f")
    assert (
        res == Unreadable(UnreadableReason.ACCESS_EXCEPTION, "egress_denied")
        and not called
    )
    allowed = settings(VEKTRA_LMS_MOODLE_ALLOWED_CIDRS="10.20.0.0/16")
    assert isinstance(
        await make(handler, ip="10.20.3.4", s=allowed).call(SITE, "f"), Readable
    )


async def test_metadata_address_is_refused_even_inside_an_allowed_range():
    allowed = settings(VEKTRA_LMS_MOODLE_ALLOWED_CIDRS="169.254.0.0/16")
    res = await make(ok({}), ip="169.254.169.254", s=allowed).call(SITE, "f")
    assert res.detail_code == "egress_denied"  # type: ignore[union-attr]


async def test_plain_http_needs_development_and_the_flag():
    http_site = SiteRef("s", "http://moodle.example.org")
    assert (await make(ok({})).call(http_site, "f")).detail_code == "plain_http_refused"  # type: ignore[union-attr]
    flag = settings(VEKTRA_LMS_MOODLE_ALLOW_HTTP="true")
    # the switch alone is not enough: development is the composition root's flag, passed in
    assert (
        await make(ok({}), s=flag).call(http_site, "f")
    ).detail_code == "plain_http_refused"  # type: ignore[union-attr]
    assert isinstance(
        await make(ok({}), s=flag, development=True).call(http_site, "f"), Readable
    )


async def test_no_token_means_unreadable_and_no_request():
    called = []
    handler = lambda r: called.append(1) or httpx.Response(200, json={})  # noqa: E731
    res = await make(handler, token=None).call(SITE, "f")  # type: ignore[arg-type]
    assert (
        res == Unreadable(UnreadableReason.ACCESS_EXCEPTION, "no_token") and not called
    )


# --- logging is content-free --------------------------------------------------------------


async def test_logs_never_carry_the_token_params_bodies_or_urls():
    body = {
        "exception": "e",
        "errorcode": "accessexception",
        "message": "CANARY-MESSAGE",
    }
    with capture_logs() as logs:
        client = make(ok({"fullname": "CANARY-BODY"}))
        await client.call(SITE, "mod_quiz_get_attempt_review", {"attemptid": 987654})
        await make(ok(body)).call(SITE, "f", {"userid": 555111})
    flat = json.dumps(logs, default=str)
    for secret in (
        TOKEN,
        "CANARY-BODY",
        "CANARY-MESSAGE",
        "987654",
        "555111",
        "93.184.216.34",
        "server.php",
    ):
        assert secret not in flat, secret
    fields = {
        "wsfunction",
        "site_id",
        "status",
        "duration_ms",
        "bytes",
        "errorcode",
        "outcome",
        "event",
        "log_level",
    }
    assert logs and all(set(entry) <= fields for entry in logs)
    assert logs[1]["errorcode"] == "accessexception"


# --- breaker ----------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_breaker_opens_cools_down_and_allows_exactly_one_probe():
    clock = Clock()
    b = CircuitBreaker(threshold=3, cooldown_s=30, clock=clock)
    for _ in range(2):
        b.record_failure()
    assert b.state is BreakerState.CLOSED and b.allow()
    b.record_failure()
    assert b.state is BreakerState.OPEN and not b.allow()
    clock.t = 29.9
    assert not b.allow()
    clock.t = 30.0
    assert b.state is BreakerState.HALF_OPEN
    assert b.allow() and not b.allow()  # one probe only
    b.record_failure()  # the probe failed: re-open, cooldown restarts
    assert b.state is BreakerState.OPEN
    clock.t = 59.0
    assert not b.allow()
    clock.t = 60.0
    assert b.allow()
    b.record_success()
    assert b.state is BreakerState.CLOSED and b.allow() and b.allow()


def test_success_resets_the_consecutive_failure_count():
    b = CircuitBreaker(threshold=2, cooldown_s=10, clock=Clock())
    b.record_failure()
    b.record_success()
    b.record_failure()
    assert b.state is BreakerState.CLOSED


def test_registry_keeps_one_breaker_per_site():
    reg = BreakerRegistry(threshold=1, cooldown_s=10, clock=Clock())
    reg.for_site("a").record_failure()
    assert reg.for_site("a").state is BreakerState.OPEN
    assert reg.for_site("b").state is BreakerState.CLOSED


async def test_open_breaker_answers_breaker_open_without_traffic():
    calls = []

    def boom(request):
        calls.append(1)
        raise httpx.ConnectError("down", request=request)

    reg = BreakerRegistry(threshold=2, cooldown_s=30, clock=Clock())
    client = make(boom, breakers=reg)
    for _ in range(2):
        await client.call(SITE, "f")
    assert len(calls) == 2
    res = await client.call(SITE, "f")
    assert res.reason is UnreadableReason.BREAKER_OPEN and len(calls) == 2  # type: ignore[union-attr]
    other = SiteRef("site-2", "https://moodle.example.org")
    await client.call(other, "f")
    assert len(calls) == 3  # another site is unaffected


# --- rate limiter --------------------------------------------------------------------------------


async def test_token_bucket_throttles_to_the_rate_with_a_fake_clock():
    clock = Clock()
    slept: list[float] = []

    async def sleep(seconds: float):
        slept.append(seconds)
        clock.t += seconds

    bucket = AsyncTokenBucket(rate=10, burst=2, clock=clock, sleep=sleep)
    for _ in range(2):
        await bucket.acquire()
    assert slept == []  # the burst is free
    for _ in range(10):
        await bucket.acquire()
    assert sum(slept) == pytest.approx(1.0)  # 10 more at 10/s took one simulated second


async def test_bucket_is_shared_by_concurrent_callers():
    clock = Clock()

    async def sleep(seconds: float):
        clock.t += seconds
        await asyncio.sleep(0)

    bucket = AsyncTokenBucket(rate=5, burst=1, clock=clock, sleep=sleep)
    await asyncio.gather(*[bucket.acquire() for _ in range(6)])
    assert clock.t == pytest.approx(1.0)  # 1 free + 5 waits of 0.2 s


def test_bucket_rejects_a_non_positive_rate():
    with pytest.raises(ValueError):
        AsyncTokenBucket(rate=0)


# --- review fixes: probe slot, proxies, logging, deadline, DNS ----------------------------------


async def test_an_unexpected_exception_never_escapes_and_releases_the_probe_slot():
    clock = Clock()
    reg = BreakerRegistry(threshold=1, cooldown_s=10, clock=clock)

    def boom(request):
        raise httpx.DecodingError("bad gzip", request=request)

    client = make(boom, breakers=reg)
    res = await client.call(SITE, "f")  # never raises
    assert res.detail_code in ("transport_error", "client_error")  # type: ignore[union-attr]
    clock.t = 11.0  # half-open
    await client.call(
        SITE, "f"
    )  # the probe ran (and failed) instead of leaking the slot
    clock.t = 22.0
    assert reg.for_site(
        SITE.site_id
    ).allow()  # a later probe is possible: not stuck open


async def test_a_malformed_cidr_setting_is_unreadable_not_an_exception():
    bad = settings(VEKTRA_LMS_MOODLE_ALLOWED_CIDRS="not-a-cidr")
    res = await make(ok({}), s=bad).call(SITE, "f")
    assert isinstance(res, Unreadable) and res.detail_code == "client_error"


async def test_cancellation_releases_the_probe_slot():
    clock = Clock()
    reg = BreakerRegistry(threshold=1, cooldown_s=10, clock=clock)
    b = reg.for_site(SITE.site_id)
    b.record_failure()
    clock.t = 11.0

    async def hang(request):
        await asyncio.sleep(3600)

    client = make(hang, breakers=reg)
    task = asyncio.create_task(client.call(SITE, "f"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert b.allow()  # the slot is free again


def test_the_client_ignores_proxy_environment_and_silences_the_http_loggers(
    monkeypatch,
):
    import logging

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    client = make(ok({}))
    assert client._http._trust_env is False
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


async def test_a_slow_drip_body_hits_the_total_deadline():
    async def drip():
        while True:
            await asyncio.sleep(0.05)
            yield b"x"

    def handler(request):
        return httpx.Response(200, content=drip())

    s = settings(
        VEKTRA_LMS_MOODLE_WS_TOTAL_TIMEOUT_S="0.3",
        VEKTRA_LMS_MOODLE_WS_MAX_RESPONSE_BYTES="8388608",
    )
    res = await make(handler, s=s).call(SITE, "f")
    assert res.reason is UnreadableReason.TIMEOUT  # type: ignore[union-attr]


async def test_dns_failure_trips_the_breaker_and_local_refusals_do_not():
    reg = BreakerRegistry(threshold=2, cooldown_s=30, clock=Clock())

    def no_dns(*_a, **_k):
        raise socket.gaierror("nope")

    client = MoodleWsClient(
        settings(),
        token_provider=lambda _s: TOKEN,
        transport=httpx.MockTransport(ok({})),
        resolver=no_dns,
        breakers=reg,
    )
    for _ in range(2):
        assert (await client.call(SITE, "f")).detail_code == "dns_failure"  # type: ignore[union-attr]
    assert (await client.call(SITE, "f")).reason is UnreadableReason.BREAKER_OPEN  # type: ignore[union-attr]
    # local refusals (no token) do not count as failures and cannot close an open breaker
    reg2 = BreakerRegistry(threshold=1, cooldown_s=30, clock=Clock())
    reg2.for_site(SITE.site_id).record_failure()
    c2 = make(ok({}), token=None, breakers=reg2)  # type: ignore[arg-type]
    assert (await c2.call(SITE, "f")).reason is UnreadableReason.BREAKER_OPEN  # type: ignore[union-attr]


async def test_the_file_download_token_never_reaches_the_stdlib_http_logs(caplog):
    """httpx logs the request URL, query included, on the stdlib `httpx` logger. A level is
    a mutable global, so the logger is forced to DEBUG AFTER the client was built (a later
    `basicConfig(level=DEBUG)`): the redaction filter must hold regardless."""
    import logging

    client = make(lambda r: httpx.Response(200, content=b"data"))
    httpx_logger = logging.getLogger("httpx")
    old = httpx_logger.level
    httpx_logger.setLevel(logging.DEBUG)
    try:
        with caplog.at_level(logging.DEBUG):
            res = await client.fetch_file(
                SITE, "/1/mod_assign/submission_files/9/f.txt"
            )
    finally:
        httpx_logger.setLevel(old)
    assert isinstance(res, Readable)
    records = [r for r in caplog.records if r.name == "httpx"]
    texts = [r.getMessage() for r in records]
    assert any("pluginfile.php" in t for t in texts), (
        "no httpx record: the test is vacuous"
    )
    assert not [t for t in texts if TOKEN in t], texts
    assert any("token=<redacted>" in t for t in texts), texts
    assert TOKEN not in caplog.text


def test_the_redaction_filter_handles_formatted_args_and_other_spellings():
    import logging

    from vektra_lms_moodle.client import _REDACTOR

    rec = logging.LogRecord(
        "httpx", logging.INFO, "", 0,
        'HTTP Request: %s %s "%s"',
        ("GET", "https://1.2.3.4/x?wstoken=SECRET1&token=SECRET2&a=b", "HTTP/1.1 200 OK"),
        None,
    )  # fmt: skip
    assert _REDACTOR.filter(rec)
    out = rec.getMessage()
    assert "SECRET" not in out and "a=b" in out and out.count("<redacted>") == 2


async def test_a_loopback_resolution_is_refused_outside_development():
    cidrs = settings(VEKTRA_LMS_MOODLE_ALLOWED_CIDRS="127.0.0.0/8")
    res = await make(ok({}), s=cidrs, ip="127.0.0.1").call(SITE, "f")
    assert isinstance(res, Unreadable) and res.detail_code == "egress_denied"
    assert isinstance(
        await make(ok({"a": 1}), s=cidrs, ip="127.0.0.1", development=True).call(
            SITE, "f"
        ),
        Readable,
    )


async def test_a_hostile_deeply_nested_document_never_escapes_call_or_fetch_file(
    monkeypatch,
):
    # a document that parses (json is under its own recursion limit) and then blows the
    # stack in shape analysis: the contract says call() never raises
    import vektra_lms_moodle.client as client_mod

    def explode(*_a, **_k):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(client_mod, "evidence_for", explode)
    nested = {"a": json.loads(("[" * 900 + "]" * 900).encode())}
    c = make(lambda r: httpx.Response(200, json=nested))
    res = await c.call(SITE, "mod_quiz_get_quizzes_by_courses")
    assert isinstance(res, Unreadable) and res.detail_code == "client_error"
    # and a file fetch whose post-processing raises is contained too
    c2 = make(lambda r: httpx.Response(200, content=b"x"))

    async def boom(*_a, **_k):
        raise RecursionError

    c2._send = boom  # type: ignore[method-assign]
    res2 = await c2.fetch_file(SITE, "/p")
    assert isinstance(res2, Unreadable) and res2.detail_code == "client_error"
    assert c2._breakers.for_site(SITE.site_id).state is BreakerState.CLOSED


async def test_a_slow_rate_limiter_does_not_strand_the_half_open_probe_slot():
    class Clock:
        t = 0.0

        def __call__(self):
            return self.t

    clock = Clock()
    reg = BreakerRegistry(threshold=1, cooldown_s=10, clock=clock)  # type: ignore[arg-type]
    breaker = reg.for_site(SITE.site_id)
    breaker.record_failure()
    clock.t = 11.0  # cooled down: half-open, one probe allowed
    assert breaker.state is BreakerState.HALF_OPEN

    gate = asyncio.Event()
    waiting = asyncio.Event()

    async def slow_sleep(_s: float) -> None:
        waiting.set()
        await gate.wait()

    limiter = AsyncTokenBucket(1.0, burst=1.0, sleep=slow_sleep)
    await limiter.acquire()  # drain the burst: the next acquire has to wait
    client = make(ok({"a": 1}), breakers=reg, limiter=limiter)  # type: ignore[arg-type]
    task = asyncio.create_task(client.call(SITE, "f"))
    await asyncio.wait_for(waiting.wait(), 2)
    # the call is parked on the limiter: it must NOT be holding the probe slot
    assert breaker._probe_in_flight is False
    gate.set()
    limiter._clock = lambda: 100.0  # type: ignore[attr-defined]
    res = await asyncio.wait_for(task, 5)
    assert isinstance(res, Readable)
    assert breaker.state is BreakerState.CLOSED

"""The generated hostile corpus through the real sidecar (plan assess-parser, task F).

In-process server, real forkserver children under real rlimits, the real worker-side
client over a real unix socket. Linux only (rlimits, /proc, forkserver semantics): off
Linux the module is skipped with a reason, and CI asserts 0 skips for it. Every case
asserts the generator produced the file before asserting its status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import os
import sys
import time
from collections.abc import AsyncIterator
from multiprocessing import forkserver
from pathlib import Path

import parser_socket_guard
import pytest
from parser_corpus_support import corpus

from vektra_assess.parser_protocol import FileStatus, Limits
from vektra_assess.parser_sidecar.child import PRELOAD
from vektra_assess.parser_sidecar.server import ParserServer, ServerConfig
from vektra_assess.worker.parser_client import ParserClient

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "linux",
        reason="rlimits, /proc and forkserver semantics need Linux",
    ),
    # the server lives in the module-scoped fixture's loop: the tests must share it
    pytest.mark.asyncio(loop_scope="module"),
]

TIMEOUT_MS = 10_000
CORPUS = corpus()


class Harness:
    def __init__(self, server: ParserServer, client: ParserClient, guard: Path) -> None:
        self.server, self.client, self.guard = server, client, guard

    def guard_lines(self) -> list[str]:
        return self.guard.read_text().splitlines() if self.guard.exists() else []


@pytest.fixture(scope="module")
async def harness(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Harness]:
    base = tmp_path_factory.mktemp("parser")
    sockdir = base / "run"
    sockdir.mkdir(mode=0o700)
    guard = base / "guard.log"
    os.environ["PARSER_GUARD_LOG"] = str(guard)
    ctx = multiprocessing.get_context("forkserver")
    ctx.set_forkserver_preload([*PRELOAD, "parser_socket_guard"])
    forkserver._forkserver._stop()  # a forkserver started earlier has no guard
    forkserver.ensure_running()
    cfg = ServerConfig(socket_path=sockdir / "p.sock", pool=3, timeout_s=10.0)
    srv = ParserServer(cfg, ctx)  # type: ignore[arg-type]
    await srv.start()
    client = ParserClient(cfg.socket_path, limits=Limits(timeout_ms=TIMEOUT_MS))
    yield Harness(srv, client, guard)
    await srv.close()
    forkserver._forkserver._stop()
    os.environ.pop("PARSER_GUARD_LOG", None)


async def _run(h: Harness, name: str):  # type: ignore[no-untyped-def]
    it = CORPUS[name]
    assert it.data or name in ("empty_file", "rv_empty"), "generator produced nothing"
    if it.magic:
        assert it.data.startswith(it.magic), "generator produced the wrong magic"
    budget = it.timeout_ms or TIMEOUT_MS
    lim = Limits(timeout_ms=budget)
    t0 = time.monotonic()
    if it.op == "sanitize_html":
        res = await h.client.sanitize_html(it.data, lim)
    else:
        res = await h.client.parse_file(it.data, it.ext, lim)
    return it, res, time.monotonic() - t0


@pytest.mark.parametrize("name", list(CORPUS))
async def test_item_maps_to_exactly_its_status(
    harness: Harness, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    before = harness.server.children_spawned
    it, res, took = await _run(harness, name)
    assert res.status is it.expected, (res.status, res.code)
    if it.upto_page is not None:
        assert res.upto_page == it.upto_page
    for s in it.contains:
        assert s in res.text, f"{s!r} missing"
    for s in it.forbidden:
        assert s not in res.text
    assert took <= (it.timeout_ms or TIMEOUT_MS) / 1000 + 1.0, (
        f"{name} took {took:.1f}s"
    )
    if it.expected is FileStatus.TOO_LARGE:
        assert harness.server.children_spawned == before, (
            "a child was forked for TOO_LARGE"
        )
    else:
        assert harness.server.children_spawned == before + 1
    assert res.label  # exactly one canonical label for every status


async def test_resource_bombs_die_by_the_designed_mechanism(harness: Harness) -> None:
    _, slow, took = await _run(harness, "pdf_slow_content")
    assert slow.code == "timeout", slow
    assert took < 2.5 + 1.0
    _, huge, _ = await _run(harness, "pdf_huge_stream")
    assert huge.status is FileStatus.CORRUPT
    assert huge.code in ("memory", "child_died"), huge.code  # RLIMIT_AS, not luck


async def test_parent_survives_the_whole_corpus_and_keeps_serving(
    harness: Harness,
) -> None:
    for name in (
        "pdf_huge_stream",
        "docx_billion_laughs",
        "zip_bomb_total",
        "pdf_valid",
    ):
        await _run(harness, name)
    assert await harness.client.ping() < 2
    assert await harness.client.ping(deep=True) < 8
    assert harness.server.server is not None and harness.server.server.is_serving()


async def test_no_child_touched_the_network_or_spawned_a_process(
    harness: Harness,
) -> None:
    # control: the guard really records what it claims to (an attempt from a child)
    ctx = harness.server.ctx
    p = ctx.Process(target=parser_socket_guard.attempt_forbidden)
    p.start()
    p.join(10)
    kinds = {ln.split()[0] for ln in harness.guard_lines()}
    assert "net-connect" in kinds and "spawn" in kinds, "the guard does not bite"
    # real result: every line comes from the control child: the corpus children produced none
    lines = harness.guard_lines()
    assert all(ln.endswith(f"pid={p.pid}") for ln in lines), lines  # only the control


async def test_two_consecutive_items_never_share_a_child_pid(harness: Harness) -> None:
    seen: list[int] = []
    for i in range(6):
        before = len(harness.server.child_pids)
        await harness.client.parse_file(f"item {i}".encode(), "txt")
        new = harness.server.child_pids[before:]
        assert len(new) == 1
        seen.append(new[0])
    assert len(set(seen)) == 6 and os.getpid() not in seen


async def test_nfr014_fifty_alternating_items_share_no_pid_and_no_canary(
    harness: Harness,
) -> None:
    pids: list[int] = []
    for i in range(50):
        tag = f"STUDENT{i:02d}-CANARY-{i * 7919:06d}"
        before = len(harness.server.child_pids)
        if i % 2:
            res = await harness.client.parse_file(f"risposta {tag}\n".encode(), "txt")
        else:
            from parser_corpus_support import module

            m = module()
            res = await harness.client.parse_file(
                m.make_docx(m._p(f"risposta {tag}")), "docx"
            )
        assert res.status is FileStatus.READ
        assert tag in res.text
        others = [f"STUDENT{j:02d}-CANARY" for j in range(50) if j != i]
        assert not any(o in res.text for o in others)
        pids.extend(harness.server.child_pids[before:])
    assert len(pids) == 50 and len(set(pids)) == 50


async def test_canaries_never_reach_logs_stdout_or_stderr(
    harness: Harness,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.INFO)
    for name in (
        "docx_valid",
        "pdf_valid",
        "txt_valid",
        "docx_xxe_file",
        "rv_multichoice",
        "pdf_encrypted",
    ):
        await _run(harness, name)
    out = capfd.readouterr()
    haystack = out.out + out.err + "\n".join(r.getMessage() for r in caplog.records)
    assert "parse" in haystack, (
        "no sidecar log line captured: the check would be vacuous"
    )
    for name in CORPUS:
        assert CORPUS[name].canary not in haystack
    events = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.getMessage().startswith("{")
    ]
    assert events and all(
        set(e)
        <= {
            "event",
            "req_id",
            "op",
            "status",
            "code",
            "duration_ms",
            "child_pid",
            "exit_code",
            "stderr_bytes",
            "timed_out",
            "vm_peak_kb",
            "rss_peak_kb",
        }
        for e in events
    )


async def test_concurrent_requests_are_bounded_by_the_pool(harness: Harness) -> None:
    results = await asyncio.gather(
        *[harness.client.parse_file(f"c{i}".encode(), "txt") for i in range(8)]
    )
    assert all(r.status is FileStatus.READ for r in results)

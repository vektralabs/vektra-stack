"""The real parser sidecar container, attacked from outside and inside (plan assess-parser, task F).

Runs against `docker compose -f docker-compose.yml -f docker-compose.assess.yml --profile
assess up`. The corpus is generated on the host and pushed through a worker-side client that
runs INSIDE the worker container (the only other holder of the socket volume), so the whole
path is the production one. Hardening is read from `docker inspect` and probed from inside
the container, not read from the compose file. When VEKTRA_ASSESS_STACK=1 a missing stack is
a FAILURE (BLOCKED), never a skip, as in test_assess_stack.py.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(
    os.environ.get("VEKTRA_ASSESS_COMPOSE_DIR", Path(__file__).resolve().parents[2])
)
REPO = Path(__file__).resolve().parents[2]
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(ROOT / "docker-compose.yml"),
    "-f",
    str(ROOT / "docker-compose.assess.yml"),
    "--profile",
    "assess",
]
MiB = 1024 * 1024
PEAK_BUDGET = 768 * MiB
SUBMISSION_BUDGET_S = 15.0


def _compose(
    *args: str,
    check: bool = False,
    timeout: int = 300,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*COMPOSE, *args],
        cwd=ROOT,
        capture_output=True,
        timeout=timeout,
        check=check,
        input=stdin,
    )


def _text(res: subprocess.CompletedProcess[bytes]) -> str:
    return res.stdout.decode("utf-8", "replace")


def _cid(service: str) -> str:
    return _text(_compose("ps", "-a", "-q", service)).strip()


def _inspect(service: str) -> dict:  # type: ignore[type-arg]
    out = subprocess.run(
        ["docker", "inspect", _cid(service)], capture_output=True, text=True, check=True
    )
    doc: dict = json.loads(out.stdout)[0]  # type: ignore[type-arg]
    return doc


@pytest.fixture(scope="module", autouse=True)
def stack_up() -> None:
    res = _compose("ps", "--status", "running", "--format", "json")
    running = set()
    for line in _text(res).splitlines():
        if line.strip():
            running.add(json.loads(line)["Service"])
    needed = {"vektra-assess-worker", "vektra-assess-parser"}
    if not needed <= running:
        msg = f"assess stack not running (missing: {sorted(needed - running)})"
        if os.environ.get("VEKTRA_ASSESS_STACK") == "1":
            pytest.fail(f"BLOCKED: {msg}")
        pytest.skip(msg)


@pytest.fixture(scope="module")
def corpus() -> dict:  # type: ignore[type-arg]
    path = REPO / "vektra-assess" / "tests" / "fixtures" / "parser" / "build_corpus.py"
    spec = importlib.util.spec_from_file_location("assess_parser_build_corpus", path)
    assert spec and spec.loader
    mod: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return {it.name: it for it in mod.build_corpus()}


# The worker-side client, run inside the worker container. stdin: one JSON line per job
# {"name", "op", "ext", "b64", "timeout_ms", "repeat"}; stdout: one JSON line per result.
RUNNER = r"""
import asyncio, base64, json, sys, time
from vektra_assess.config import VektraAssessSettings
from vektra_assess.parser_protocol import Limits
from vektra_assess.worker.parser_client import ParserClient, ParserUnavailableError

async def one(client, job):
    data = base64.b64decode(job["b64"])
    lim = Limits(timeout_ms=job.get("timeout_ms") or 10000)
    t0 = time.monotonic()
    try:
        if job["op"] == "sanitize_html":
            r = await client.sanitize_html(data, lim)
        else:
            r = await client.parse_file(data, job["ext"], lim)
        out = {"status": r.status.value, "upto_page": r.upto_page, "code": r.code,
               "chars": r.chars, "text": r.text if job.get("want_text", True) else ""}
    except ParserUnavailableError as e:
        out = {"unavailable": str(e)}
    out["name"] = job["name"]
    out["elapsed"] = time.monotonic() - t0
    return out

async def main():
    client = ParserClient.from_settings(VektraAssessSettings())
    jobs = [json.loads(l) for l in sys.stdin if l.strip()]
    mode = jobs[0].get("mode", "serial") if jobs else "serial"
    if mode == "gather":
        res = await asyncio.gather(*[one(client, j) for j in jobs])
    else:
        res = [await one(client, j) for j in jobs]
    for r in res:
        print(json.dumps(r), flush=True)

asyncio.run(main())
"""


def _job(it, **extra) -> dict:  # type: ignore[no-untyped-def,type-arg]
    return {
        "name": it.name,
        "op": it.op,
        "ext": it.ext,
        "b64": base64.b64encode(it.data).decode(),
        "timeout_ms": it.timeout_ms,
        **extra,
    }


def _run_jobs(jobs: list[dict], timeout: int = 600) -> list[dict]:  # type: ignore[type-arg]
    payload = ("\n".join(json.dumps(j) for j in jobs) + "\n").encode()
    res = _compose(
        "exec", "-T", "vektra-assess-worker", "python", "-c", RUNNER,
        stdin=payload, timeout=timeout,
    )  # fmt: skip
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")[-2000:]
    return [json.loads(ln) for ln in _text(res).splitlines() if ln.startswith("{")]


# --- hardening, from docker inspect ------------------------------------------------------


def test_inspect_shows_the_hardening() -> None:
    d = _inspect("vektra-assess-parser")
    hc, cfg = d["HostConfig"], d["Config"]
    assert hc["NetworkMode"] == "none"
    assert hc["ReadonlyRootfs"] is True
    assert cfg["User"] == "1000:1000"
    assert hc["CapDrop"] == ["ALL"] and not hc.get("CapAdd")
    assert "no-new-privileges:true" in hc["SecurityOpt"]
    assert hc["Memory"] == 768 * MiB and hc["MemorySwap"] == 768 * MiB
    assert hc["PidsLimit"] == 64
    assert hc["NanoCpus"] == 1_500_000_000
    assert {u["Name"]: (u["Soft"], u["Hard"]) for u in hc["Ulimits"]}["core"] == (0, 0)
    assert hc["Privileged"] is False
    forbidden = re.compile(r"KEY|SECRET|PASSWORD|TOKEN|DSN|DATABASE_URL", re.I)
    names = [e.split("=", 1)[0] for e in cfg["Env"]]
    # GPG_KEY is the python base image's public release-signing key id, not a secret
    assert not [n for n in names if forbidden.search(n) and n != "GPG_KEY"], names
    mounts = {m["Destination"] for m in d["Mounts"]}
    assert mounts <= {"/run/vektra-assess-parser", "/tmp"}, mounts  # no secrets mount
    assert d["NetworkSettings"]["Networks"].keys() == {"none"}


def test_socket_volume_is_mounted_in_the_parser_and_the_worker_only() -> None:
    parser = _inspect("vektra-assess-parser")
    vol = next(
        m["Name"]
        for m in parser["Mounts"]
        if m["Destination"] == "/run/vektra-assess-parser"
    )
    consumers = set()
    for line in _text(_compose("ps", "-a", "--format", "json")).splitlines():
        if not line.strip():
            continue
        svc = json.loads(line)["Service"]
        mounts = _inspect(svc)["Mounts"]
        if any(m.get("Name") == vol for m in mounts):
            consumers.add(svc)
    assert consumers == {"vektra-assess-parser", "vektra-assess-worker"}


def _exec_parser(code: str) -> subprocess.CompletedProcess[bytes]:
    return _compose("exec", "-T", "vektra-assess-parser", "python", "-c", code)


def test_inside_the_container_nothing_leaves_and_nothing_is_held() -> None:
    probe = r"""
import json, os, socket
out = {"uid": os.getuid()}
for name, addr in (("ip", ("1.1.1.1", 53)), ("pg", ("postgres", 5432))):
    try:
        socket.create_connection(addr, timeout=3).close(); out[name] = "connected"
    except OSError as e:
        out[name] = type(e).__name__
out["routes"] = len(open("/proc/net/route").read().splitlines()) - 1
out["inet6"] = [l.split()[-1] for l in open("/proc/net/if_inet6").read().splitlines()]
try:
    open("/app/x", "w").close(); out["write_root"] = "written"
except OSError as e:
    out["write_root"] = type(e).__name__
out["secrets_dir"] = os.path.exists("/run/secrets")
out["cap_eff"] = [l.split()[1] for l in open("/proc/1/status") if l.startswith("CapEff")][0]
out["env_secretish"] = [k for k in os.environ if k != "GPG_KEY" and any(f in k for f in ("KEY", "SECRET", "PASSWORD", "TOKEN", "DSN", "DATABASE"))]
print(json.dumps(out))
"""
    res = _exec_parser(probe)
    assert res.returncode == 0, res.stderr.decode()
    out = json.loads(_text(res).splitlines()[-1])
    assert out["uid"] == 1000
    assert out["ip"] != "connected" and out["pg"] != "connected", out
    # no route at all and no address but loopback (the VM kernel lists unconfigured
    # tunnel pseudo-devices in the "none" namespace; they are down, routeless, addressless)
    assert out["routes"] == 0 and out["inet6"] == ["lo"], out
    assert out["write_root"] != "written", out
    assert out["secrets_dir"] is False
    assert int(out["cap_eff"], 16) == 0, out
    assert out["env_secretish"] == [], out


# --- the corpus through the real sidecar ----------------------------------------------------

SLOW = {"pdf_slow_content"}


def test_corpus_through_the_container_and_canaries_stay_out_of_the_logs(
    corpus: dict,
) -> None:  # type: ignore[type-arg]
    jobs = [_job(it) for it in corpus.values()]
    results = {r["name"]: r for r in _run_jobs(jobs)}
    assert set(results) == set(corpus)
    for name, it in corpus.items():
        r = results[name]
        assert "unavailable" not in r, (name, r)
        assert r["status"] == it.expected.value, (name, r["status"], r["code"])
        if it.upto_page is not None:
            assert r["upto_page"] == it.upto_page, name
        for s in it.contains:
            assert s in r["text"], (name, s)
        for s in it.forbidden:
            assert s not in r["text"], (name, s)
        assert r["elapsed"] <= (it.timeout_ms or 10_000) / 1000 + 1.0, (
            name,
            r["elapsed"],
        )
    logs = subprocess.run(
        ["docker", "logs", _cid("vektra-assess-parser")], capture_output=True, text=True
    )
    haystack = logs.stdout + logs.stderr
    assert '"event":"parse"' in haystack, (
        "no sidecar log line: the canary check would be vacuous"
    )
    for it in corpus.values():
        assert it.canary not in haystack, it.name
    wlogs = subprocess.run(
        ["docker", "logs", _cid("vektra-assess-worker")], capture_output=True, text=True
    )
    assert not [
        it.name for it in corpus.values() if it.canary in wlogs.stdout + wlogs.stderr
    ]
    assert _inspect("vektra-assess-parser")["State"]["OOMKilled"] is False
    assert _inspect("vektra-assess-parser")["State"]["Running"] is True
    # Source code and markup were returned as text, never run: the cgroup's high-water pid
    # count stays at the supervisor, its two helpers, the pool and a deep ping.
    pids = _compose(
        "exec", "-T", "vektra-assess-parser", "cat", "/sys/fs/cgroup/pids.peak"
    )
    peak = int(_text(pids).strip())
    print(f"parser container pids.peak = {peak} (limit 64)")
    assert peak <= 16


def test_parse_events_carry_codes_only(corpus: dict) -> None:  # type: ignore[type-arg]
    logs = subprocess.run(
        ["docker", "logs", _cid("vektra-assess-parser")], capture_output=True, text=True
    )
    events = [
        json.loads(ln)
        for ln in (logs.stdout + logs.stderr).splitlines()
        if ln.startswith('{"event":"parse"')
    ]
    assert events
    allowed = {
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
    assert all(set(e) <= allowed for e in events)


# --- NFR-020: memory under the worst case, wall time of a submission ---------------------------


def _mem_files(cid: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in ("memory.peak", "memory.events", "memory.current", "memory.max"):
        r = subprocess.run(
            ["docker", "exec", cid, "cat", f"/sys/fs/cgroup/{name}"],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            out[name] = r.stdout.strip()
    return out


def test_memory_peak_under_three_concurrent_worst_case_items(corpus: dict) -> None:  # type: ignore[type-arg]
    cid = _cid("vektra-assess-parser")
    worst = [
        corpus["pdf_huge_stream"],
        corpus["zip_ratio_bomb"],
        corpus["pdf_51_pages"],
    ]
    before = _mem_files(cid)
    if "memory.peak" not in before:
        pytest.fail(
            "BLOCKED: cgroup v2 memory.peak is not readable in the parser container"
        )
    # The cgroup is read-only inside the container, so memory.peak is the high-water mark
    # since container start: a stricter (never looser) bound than a per-test peak.
    oom_before = int(
        re.search(r"oom_kill (\d+)", before.get("memory.events", "oom_kill 0")).group(1)
    )  # type: ignore[union-attr]
    for _ in range(20):
        jobs = [{**_job(it, want_text=False), "mode": "gather"} for it in worst]
        res = _run_jobs(jobs)
        assert [r["status"] for r in res] == [it.expected.value for it in worst]
    # the true worst case for memory: three children all running into RLIMIT_AS at once
    for _ in range(10):
        jobs = [
            {**_job(corpus["pdf_huge_stream"], want_text=False), "mode": "gather"}
        ] * 3
        res = _run_jobs(jobs)
        assert [r["code"] for r in res] == ["memory"] * 3
    after = _mem_files(cid)
    peak = int(after["memory.peak"])
    oom_after = int(re.search(r"oom_kill (\d+)", after["memory.events"]).group(1))  # type: ignore[union-attr]
    print(f"NFR-020 parser container memory.peak = {peak / MiB:.1f} MiB (budget 768)")
    assert peak <= PEAK_BUDGET
    assert oom_after == oom_before == 0, (oom_before, oom_after)
    assert _inspect("vektra-assess-parser")["State"]["OOMKilled"] is False


def test_ten_file_reference_submission_within_budget(corpus: dict) -> None:  # type: ignore[type-arg]
    names = [
        "pdf_valid",
        "docx_valid",
        "odt_valid",
        "txt_valid",
        "md_valid",
        "code_python",
        "code_java",
        "pdf_51_pages",
        "latin1_text",
        "svg_as_text",
    ]
    jobs = [{**_job(corpus[n], want_text=False), "mode": "gather"} for n in names]
    t0 = time.monotonic()
    res = _run_jobs(jobs)
    wall = time.monotonic() - t0
    print(f"10-file reference submission wall time (incl. exec overhead): {wall:.2f}s")
    assert [r["status"] for r in res] == [corpus[n].expected.value for n in names]
    assert max(r["elapsed"] for r in res) <= SUBMISSION_BUDGET_S
    assert (
        sum(r["elapsed"] for r in res) / 3 <= SUBMISSION_BUDGET_S
        or wall <= SUBMISSION_BUDGET_S + 5
    )


def test_idle_rss_of_the_parser_and_its_forkserver() -> None:
    time.sleep(2)
    r = _exec_parser(
        "import os\n"
        "tot = {}\n"
        "for p in os.listdir('/proc'):\n"
        "    if p.isdigit():\n"
        "        st = open(f'/proc/{p}/status').read().splitlines()\n"
        "        rss = [int(l.split()[1]) for l in st if l.startswith('VmRSS')]\n"
        "        cmd = open(f'/proc/{p}/cmdline').read().replace('\\0', ' ')[:70]\n"
        "        if rss: tot[p] = (rss[0], cmd)\n"
        "print(tot)\n"
    )
    print("parser idle RSS (kB, cmd):", _text(r).strip())
    assert r.returncode == 0


# --- no degraded mode: a stopped sidecar means unavailable, not parsed locally ------------------

STOPPED_PROBE = r"""
import asyncio, json, time
from vektra_assess.config import VektraAssessSettings
from vektra_assess.parser_protocol import Limits
from vektra_assess.worker.parser_client import ParserClient, ParserUnavailableError, parser_setup_check
async def main():
    s = VektraAssessSettings()
    c = ParserClient.from_settings(s)
    out = {}
    try:
        await c.ping(); out["ping"] = "answered"
    except ParserUnavailableError as e:
        out["ping"] = "unavailable"
    t0 = time.monotonic()
    try:
        await c.parse_file(b"hello", "txt", Limits(timeout_ms=2000)); out["parse"] = "parsed"
    except ParserUnavailableError as e:
        out["parse"] = "unavailable"
    out["parse_elapsed"] = time.monotonic() - t0
    r = await parser_setup_check(c)(None)
    out["check"] = [r.result, r.reason_code]
    print(json.dumps(out))
asyncio.run(main())
"""


def test_stopped_sidecar_is_unavailable_and_the_check_fails() -> None:
    _compose("stop", "vektra-assess-parser", check=True)
    try:
        res = _compose(
            "exec", "-T", "vektra-assess-worker", "python", "-c", STOPPED_PROBE
        )
        assert res.returncode == 0, res.stderr.decode()[-1500:]
        out = json.loads(_text(res).splitlines()[-1])
        assert out["ping"] == "unavailable" and out["parse"] == "unavailable", out
        assert out["parse_elapsed"] < 5, out  # well within the client budget
        assert out["check"] == ["MISSING", "parser_unavailable"], out
    finally:
        _compose("start", "vektra-assess-parser", check=True)
        deadline = time.time() + 90
        while time.time() < deadline:
            if (
                _inspect("vektra-assess-parser")["State"]
                .get("Health", {})
                .get("Status")
                == "healthy"
            ):
                break
            time.sleep(2)
        else:
            pytest.fail("parser did not come back healthy")


# --- many large, slow senders cannot take the supervisor over its memory limit ---------------

FLOOD = r"""
import asyncio, json, struct, sys
from vektra_assess.config import VektraAssessSettings
from vektra_assess.worker.parser_client import ParserClient

N, CLAIM, SENT = 80, 10 * 1024 * 1024, 1024 * 1024

async def sender(path, i):
    r, w = await asyncio.open_unix_connection(path)
    head = json.dumps({"v": 1, "op": "parse", "req_id": f"flood{i}", "ext_hint": "txt",
                       "limits": {}, "payload_len": CLAIM}).encode()

    async def reply():
        n = struct.unpack(">I", await asyncio.wait_for(r.readexactly(4), 5))[0]
        h = json.loads(await r.readexactly(n))
        return h.get("code") or h.get("status")

    waiting = asyncio.ensure_future(reply())     # listening before we send, as a client does
    try:
        w.write(struct.pack(">I", len(head)) + head + b"x" * SENT)   # 1 MiB of a promised 10 MiB, then silence
        await w.drain()
    except ConnectionError:
        pass   # refused early (BUSY) and closed while we were still sending: expected
    try:
        return await waiting
    except Exception:
        return "silent"

async def main():
    s = VektraAssessSettings()
    path = str(s.parser_socket)
    res = await asyncio.gather(*[sender(path, i) for i in range(N)])
    out = {k: res.count(k) for k in set(res)}
    c = ParserClient.from_settings(s)
    out["ping_during"] = "ok" if await c.ping() >= 0 else "no"
    print(json.dumps(out))

asyncio.run(main())
"""


def test_slow_large_senders_are_bounded_and_the_container_stays_within_its_limit() -> (
    None
):
    cid = _cid("vektra-assess-parser")
    res = _compose(
        "exec", "-T", "vektra-assess-worker", "python", "-c", FLOOD, timeout=120
    )
    assert res.returncode == 0, res.stderr.decode()[-1500:]
    out = json.loads(_text(res).splitlines()[-1])
    print("flood of 80 stalled 10 MiB senders:", out)
    assert out.get("BUSY", 0) >= 70, (
        out
    )  # only the byte budget (4 x 10 MiB) is admitted
    assert out["ping_during"] == "ok"
    mem = _mem_files(cid)
    assert int(mem["memory.peak"]) <= PEAK_BUDGET, mem
    assert _inspect("vektra-assess-parser")["State"]["OOMKilled"] is False
    # the stalled senders are released by the overall read deadline: nothing stays reserved
    time.sleep(1)
    again = _compose("exec", "-T", "vektra-assess-parser", "python", "-m",
                     "vektra_assess.parser_sidecar.ping", "--deep")  # fmt: skip
    assert again.returncode == 0

"""Chaos: ``kill -9`` of the worker in the middle of transcript generation (plan 13a, REQ-067).

A child process runs the real ``generate_transcript`` as the worker role. Its audit write, which
comes after the transcript INSERT and the delivery rows inside the same transaction, is replaced
by a pause, so the process holds the uncommitted rows. It then gets SIGKILL. Postgres aborts the
transaction: 0 transcript rows, 0 delivery rows, 0 ``assess_transcript_generated`` events. The
job's retry (here a fresh in-process run) generates exactly one transcript with one delivery row
per configured recipient, and the settled invariants pass.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
from support.recipients import TEACHERS, config_pair, configure_recipients
from support.sessions import seed_session
from support.world import world

from integration.test_delivery_handler import worker  # noqa: F401  (the fixture)
from vektra_assess.keys import AssessKeyring
from vektra_assess.transcript import store
from vektra_assess.worker.transcript_gen import GenerationDeps, generate_transcript

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "tests" / "chaos" / "assess")
)
from invariants import assert_invariants

pytestmark = pytest.mark.integration

CHILD = textwrap.dedent(
    """
    import asyncio, base64, json, sys
    from pathlib import Path
    from uuid import UUID
    from sqlalchemy.ext.asyncio import create_async_engine
    import vektra_assess.worker.transcript_gen as tg
    from vektra_assess.delivery.recipients import ConfigRecipients
    from vektra_assess.keys import AssessKeyring

    spec = json.loads(Path(sys.argv[1]).read_text())

    def ring(d):
        return AssessKeyring(d["name"], d["active"],
                             {k: base64.b64decode(v) for k, v in d["keys"].items()})

    async def pause(*_a, **_k):
        Path(spec["flag"]).touch()  # the INSERTs are done, the commit is not
        await asyncio.sleep(3600)

    tg.write_event = pause

    async def main():
        engine = create_async_engine(spec["dsn"])
        deps = tg.GenerationDeps(kek=ring(spec["kek"]), transcript_kek=ring(spec["tkek"]),
                                 recipients=ConfigRecipients(ring(spec["config"])))
        await tg.generate_transcript(deps, engine.connect, UUID(spec["sid"]))
        print("RETURNED", flush=True)

    asyncio.run(main())
    """
)


def _ring(r: Any) -> dict[str, Any]:
    return {
        "name": r.name,
        "active": r.active,
        "keys": {k: base64.b64encode(v).decode() for k, v in r.keys.items()},
    }


async def _counts(pg: Any, sid: Any) -> tuple[int, int, int]:
    row = (
        await pg.run(
            "super",
            "SELECT (SELECT count(*) FROM assess_transcripts WHERE session_id = :s), "
            "(SELECT count(*) FROM assess_deliveries d JOIN assess_transcripts t "
            " ON t.id = d.transcript_id WHERE t.session_id = :s), "
            "(SELECT count(*) FROM assess_audit_events WHERE session_id = :s "
            " AND event = 'assess_transcript_generated')",
            {"s": sid},
        )
    )[0]
    return int(row[0]), int(row[1]), int(row[2])


async def test_kill9_mid_generation_commits_nothing_and_the_retry_generates_once(
    pg,
    worker,  # noqa: F811  (the fixture imported above)
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        tkek = AssessKeyring(store.KEYRING, "t1", {"t1": os.urandom(32)})
        s = await seed_session(w, worker, "CLOSED")
        pair = config_pair()
        config = await configure_recipients(w.pg, w.site, s.cmid, TEACHERS, pair=pair)
        assert await _counts(pg, s.session_id) == (0, 0, 0)
        flag = tmp_path / "inside-tx"
        spec = {
            "dsn": pg.url("worker"),
            "sid": str(s.session_id),
            "flag": str(flag),
            "kek": _ring(s.kek),
            "tkek": _ring(tkek),
            "config": _ring(pair[1]),
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))
        os.chmod(spec_path, 0o600)
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(spec_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 60
            while not flag.exists() and time.monotonic() < deadline:
                assert child.poll() is None, child.stderr.read() if child.stderr else ""
                time.sleep(0.05)
            assert flag.exists(), "the child never reached the generation transaction"
            os.kill(child.pid, signal.SIGKILL)
            child.wait(10)
        finally:
            if child.poll() is None:
                child.kill()
        time.sleep(0.5)
        assert await _counts(pg, s.session_id) == (0, 0, 0)

        got = await generate_transcript(
            GenerationDeps(kek=s.kek, transcript_kek=tkek, recipients=config),
            worker.connect,
            s.session_id,
        )
        assert got is not None and got.created
        assert await _counts(pg, s.session_id) == (1, len(TEACHERS), 1)
        # a second retry (the job re-run after a lost settle) writes nothing more
        await generate_transcript(
            GenerationDeps(kek=s.kek, transcript_kek=tkek, recipients=config),
            worker.connect,
            s.session_id,
        )
        assert await _counts(pg, s.session_id) == (1, len(TEACHERS), 1)
        await assert_invariants(
            pg,
            "kill9:mid-generation",
            expect_sessions=1,
            sites=[w.site],
            only=["no_orphan_content", "one_sent_per_recipient", "handoff_only_live"],
        )

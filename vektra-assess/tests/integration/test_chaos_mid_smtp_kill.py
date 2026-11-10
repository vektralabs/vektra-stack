"""Chaos: ``kill -9`` of the delivering process between DATA and the 250 (plan 13a, REQ-075).

The in-process crash test (``test_delivery_handler``) raises an exception after the relay said
250. This one kills a real OS process, with no handler, no ``finally`` and no rollback:

1. a child process runs the real ``deliver_transcript`` as the worker role against the TLS mail
   sink, with the sink storing each message and withholding its 250;
2. as soon as the sink holds a message the child gets SIGKILL: the relay has the copy, the
   process never heard 250, and its claim is committed with no outcome;
3. once the claim is stale, a fresh run resends with the same Message-ID.

Pass, by rows and sink copies: exactly one SENT row per recipient. A message the killed process
had handed over shows 2 copies on the sink, with an audited ``assess_delivery_possible_duplicate``
for its row; no message has more than 2. The suite's invariant checker runs over the result.
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
from fixtures.mailsink import PASSWORD, USERNAME, MailSink
from support.world import world

from integration.test_delivery_handler import (  # type: ignore[attr-defined]
    STALE,
    Clock,
    _deps,
    _ready,
    events,
    rows,
    worker,  # noqa: F401  (the fixture)
)
from vektra_assess.delivery import handler as h

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "tests" / "chaos" / "assess")
)
from invariants import assert_invariants

pytestmark = pytest.mark.integration

CHILD = textwrap.dedent(
    """
    import asyncio, base64, json, sys
    from datetime import timedelta
    from pathlib import Path
    from uuid import UUID
    from sqlalchemy.ext.asyncio import create_async_engine
    from vektra_assess.delivery import handler as h
    from vektra_assess.delivery.backoff import parse_schedule
    from vektra_assess.delivery.recipients import ConfigRecipients
    from vektra_assess.delivery.smtp import SmtpConfig, SmtpSender
    from vektra_assess.keys import AssessKeyring

    spec = json.loads(Path(sys.argv[1]).read_text())

    def ring(d):
        return AssessKeyring(d["name"], d["active"],
                             {k: base64.b64decode(v) for k, v in d["keys"].items()})

    async def main():
        engine = create_async_engine(spec["dsn"])
        deps = h.DeliveryDeps(
            transcript_kek=ring(spec["transcript"]),
            recipients=ConfigRecipients(ring(spec["config"])),
            sender=SmtpSender(SmtpConfig(**{**spec["smtp"], "ca_file": Path(spec["smtp"]["ca_file"])})),
            sender_address="vektra@vektra.example.org",
            message_id_domain="vektra.example.org",
            domains=frozenset(["uni.example"]),
            schedule=parse_schedule("1m,5m,15m,1h"),
            window=timedelta(hours=72),
            stale_after=timedelta(seconds=spec["stale_s"]),
        )
        print("READY", flush=True)
        await h.deliver_transcript(deps, engine.connect, UUID(spec["tid"]))
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


async def test_kill9_between_data_and_250_leaves_one_sent_row_per_recipient(
    pg,
    worker,  # noqa: F811  (the fixture imported above)
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    sink = MailSink(tmp_path / "sink").start()
    try:
        async with world(pg) as w:
            s, ring, config, pair = await _ready(w, worker)
            sink.fault = "hold_after_data"
            spec = {
                "dsn": pg.url("worker"),
                "tid": str(s.session_id),
                "stale_s": STALE.total_seconds(),
                "transcript": _ring(ring),
                "config": _ring(pair[1]),
                "smtp": {
                    "host": "127.0.0.1",
                    "port": sink.port,
                    "sender": "vektra@vektra.example.org",
                    "tls_mode": "starttls",
                    "username": USERNAME,
                    "password": PASSWORD,
                    "timeout_s": 30.0,
                    "ca_file": str(sink.ca_file),
                    "max_connections": 2,
                },
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
                assert sink.held.wait(60), "the child never reached DATA"
                time.sleep(0.5)  # let the second row reach DATA too, if it is going to
                os.kill(child.pid, signal.SIGKILL)
                child.wait(10)
            finally:
                if child.poll() is None:
                    child.kill()
            out = child.stdout.read() if child.stdout else ""
            assert "RETURNED" not in out, "the child finished before the kill"
            held = len(sink.messages)
            assert held >= 1
            sink.fault = None
            sink.release.set()

            stuck = await rows(pg, s.session_id)
            assert {r.status for r in stuck} <= {"PENDING"}
            claimed = [r for r in stuck if r.attempt_started_at is not None]
            assert len(claimed) >= held, (
                "every message on the sink belongs to a claimed row"
            )

            clock = await Clock().sync(pg)
            clock.now += STALE + STALE  # past the dead process's lease
            deps = _deps(ring, config, sink, clock)
            got = await h.deliver_transcript(deps, worker.connect, s.session_id)
            assert got is not None

            final = await rows(pg, s.session_id)
            assert [r.status for r in final] == ["SENT", "SENT"]
            dup = await events(pg, s.session_id, "assess_delivery_possible_duplicate")
            assert len(dup) == len(claimed)
            for r in final:
                copies = len(sink.by_message_id(r.message_id))
                assert 1 <= copies <= 2, (r.message_id, copies)
            await assert_invariants(
                pg,
                "kill9:mid-smtp",
                expect_sessions=1,
                sites=[w.site],
                only=["one_sent_per_recipient", "no_orphan_content"],
                sink_copies=lambda mid: len(sink.by_message_id(mid)),
            )
    finally:
        sink.stop()

"""The real worker process against a real Postgres (plan assess-job-loop, group C/D).

Starts ``python -m vektra_app.assess_worker`` as the deployment does (development override for the
unbound slots), and checks from the outside: it reaches ``ready``, a job of an unbound kind fails
closed (dead, ``handler_not_bound``, one audit event), its connection count stays within the
5 + 1 budget, and SIGTERM ends it with exit code 0 inside the 30 s grace period and a ``stopped``
heartbeat. This subprocess spawn imports nothing from ``vektra_app``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy.engine import URL, make_url

pytestmark = pytest.mark.integration

ROLE_PASSWORD = "test-role-password-0123456789"


def _worker_secrets(d: Path) -> Path:
    from vektra_assess.keys import KEY_MATRIX, Need, generate

    full = d.parent / f"full-{d.name}"
    generate(full)
    d.mkdir()
    for name, spec in KEY_MATRIX.items():
        if spec.worker is Need.REQUIRED:
            (d / name).write_bytes((full / name).read_bytes())
            os.chmod(d / name, 0o400)
    pw = d / "assess_db_worker_password"
    os.chmod(
        pw, 0o600
    )  # read-only after the generator; rewrite with the fixture's role password
    pw.write_text(ROLE_PASSWORD)
    os.chmod(pw, 0o400)
    return d


async def _until(cond, timeout_s: float, what: str) -> None:
    async with asyncio.timeout(timeout_s):
        while not await cond():
            await asyncio.sleep(0.2)


async def test_worker_process_runs_fails_closed_and_exits_zero_on_sigterm(
    pg, tmp_path: Path
):
    await pg.run("super", "DELETE FROM assess_jobs")
    await pg.run("super", "DELETE FROM assess_heartbeat")
    audit_before = (await pg.run("super", "SELECT count(*) FROM assess_audit_events"))[
        0
    ][0]
    secrets = _worker_secrets(tmp_path / "w")
    hb_file = tmp_path / "hb"
    u = make_url(pg.super_url)
    worker_url = URL.create(
        u.drivername, "vektra_assess_worker", None, u.host, u.port, u.database
    ).render_as_string(hide_password=False)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("VEKTRA_")},
        "VEKTRA_ENV": "development",
        "VEKTRA_ASSESS_ENABLED": "true",
        # the deployment's provider (ADR-0033 D2), loaded by entry point; nothing connects
        "VEKTRA_ASSESS_LMS_PROVIDER": "moodle",
        "VEKTRA_ASSESS_SECRETS_DIR": str(secrets),
        "VEKTRA_ASSESS_WORKER_DATABASE_URL": worker_url,
        "VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND": "true",
        # a fully keyed worker binds the pre-flight, which builds the Part B model at startup
        # (plan assess-session-lifecycle): it needs a model id, never called by this test
        "VEKTRA_ASSESS_LLM_MODEL": "ollama/dev-unactivated",
        "VEKTRA_ASSESS_WORKER_HEARTBEAT_FILE": str(hb_file),
        "VEKTRA_ASSESS_WORKER_HEARTBEAT_INTERVAL_S": "1",
        "VEKTRA_ASSESS_JOB_POLL_INTERVAL_S": "0.2",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "vektra_app.assess_worker"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:

        async def ready() -> bool:
            if proc.poll() is not None:
                raise AssertionError(
                    f"worker exited early ({proc.returncode}): {proc.stdout.read()}"
                )
            rows = await pg.run(
                "super", "SELECT details->>'state' FROM assess_heartbeat"
            )
            return bool(rows) and rows[0][0] == "ready"

        await _until(ready, 90, "worker ready")
        assert hb_file.exists(), "the healthcheck file is touched"

        await pg.run(
            "super",
            # a slot this worker leaves unbound: no relay is configured, so delivery is not
            # bound (a production worker binds every slot since 0044 dropped rotate_keys)
            "INSERT INTO assess_jobs (kind, args, max_attempts) "
            "VALUES ('transcript_deliver', '{}'::jsonb, 1)",
        )
        await pg.run("super", "SELECT pg_notify('assess_jobs', '')")

        async def dead() -> bool:
            rows = await pg.run(
                "super", "SELECT state, last_error_code FROM assess_jobs"
            )
            return bool(rows) and rows[0][0] == "dead"

        await _until(dead, 20, "unbound job dead")
        row = (
            await pg.run(
                "super", "SELECT state, last_error_code, attempts FROM assess_jobs"
            )
        )[0]
        assert tuple(row) == ("dead", "handler_not_bound", 1), (
            "fail closed, never completed"
        )
        audit = await pg.run(
            "super",
            "SELECT event, reason_code, actor_kind, data->>'kind' FROM assess_audit_events "
            "ORDER BY id DESC LIMIT 1",
        )
        assert tuple(audit[0]) == (
            "assess_job_dead",
            "handler_not_bound",
            "worker",
            "transcript_deliver",
        )
        assert (await pg.run("super", "SELECT count(*) FROM assess_audit_events"))[0][
            0
        ] == audit_before + 1

        conns = (
            await pg.run(
                "super",
                "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'vektra-assess-worker%'",
            )
        )[0][0]
        assert 1 <= conns <= 6, f"connection budget is 5 + 1, saw {conns}"

        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=30)
        assert code == 0, proc.stdout.read()
        assert time.monotonic() - t0 < 30
        state = (
            await pg.run("super", "SELECT details->>'state' FROM assess_heartbeat")
        )[0][0]
        assert state == "stopped"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

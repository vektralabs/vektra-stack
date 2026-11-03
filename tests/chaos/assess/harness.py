"""Helpers of the chaos harness: secrets, worker subprocesses, kill points, row-count asserts.

Every assertion reads rows (jobs, effects), never a status field or a log line: a ``done`` job whose
effect is missing, or an effect written twice, is exactly what these tests exist to catch.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL, make_url

HERE = Path(__file__).resolve().parent
WORKER_MAIN = HERE / "worker_main.py"
ROLE_PASSWORD = "test-role-password-0123456789"


def worker_secrets(d: Path) -> Path:
    """A secrets directory holding exactly what the worker role needs (KEY_MATRIX)."""
    from vektra_assess.keys import KEY_MATRIX, Need, generate

    full = d.parent / f"full-{d.name}"
    generate(full)
    d.mkdir()
    for name, spec in KEY_MATRIX.items():
        if spec.worker is Need.REQUIRED:
            (d / name).write_bytes((full / name).read_bytes())
            os.chmod(d / name, 0o400)
    pw = d / "assess_db_worker_password"
    os.chmod(pw, 0o600)
    pw.write_text(ROLE_PASSWORD)
    os.chmod(pw, 0o400)
    return d


@dataclass
class Worker:
    proc: subprocess.Popen[str]
    name: str
    chaos_dir: Path

    def started(self) -> list[Path]:
        return sorted(self.chaos_dir.glob("started-*"))

    def kill9(self) -> None:
        """SIGKILL: no handler, no drain, no lease release, no heartbeat ``stopped``."""
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=10)

    def sigterm(self, timeout_s: float = 30.0) -> int:
        self.proc.send_signal(signal.SIGTERM)
        return self.proc.wait(timeout=timeout_s)

    def output(self) -> str:
        return self.proc.stdout.read() if self.proc.stdout else ""

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)


def start_worker(
    pg: Any, tmp: Path, name: str, secrets: Path, *, chaos_dir: Path, **env: str
) -> Worker:
    u = make_url(pg.super_url)
    dsn = URL.create(
        u.drivername, "vektra_assess_worker", None, u.host, u.port, u.database
    ).render_as_string(hide_password=False)
    chaos_dir.mkdir(exist_ok=True)
    full_env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("VEKTRA_")},
        "VEKTRA_ENV": "development",
        "VEKTRA_ASSESS_ENABLED": "true",
        "VEKTRA_ASSESS_SECRETS_DIR": str(secrets),
        "VEKTRA_ASSESS_WORKER_DATABASE_URL": dsn,
        "VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND": "true",
        "VEKTRA_ASSESS_WORKER_HEARTBEAT_FILE": str(tmp / f"hb-{name}"),
        "VEKTRA_ASSESS_WORKER_HEARTBEAT_INTERVAL_S": "1",
        "VEKTRA_ASSESS_JOB_POLL_INTERVAL_S": "0.1",
        # shortened budgets so lease expiry fits in a test; lease >= 2 x budget is still enforced
        "VEKTRA_ASSESS_JOB_PURGE_BUDGET_S": "4",
        "VEKTRA_ASSESS_JOB_PURGE_LEASE_S": "8",
        "VEKTRA_ASSESS_POOL_MAINTENANCE_CONCURRENCY": "6",
        "VEKTRA_ASSESS_JOB_BACKOFF_BASE_S": "0.2",
        "VEKTRA_ASSESS_JOB_BACKOFF_CAP_S": "1",
        "CHAOS_DIR": str(chaos_dir),
        **env,
    }
    proc = subprocess.Popen(
        [sys.executable, str(WORKER_MAIN)],
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return Worker(proc, name, chaos_dir)


async def wait_until(
    cond: Any, timeout_s: float, what: str, worker: Worker | None = None
) -> None:
    import asyncio

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if worker is not None and worker.proc.poll() is not None:
            raise AssertionError(
                f"{what}: worker {worker.name} exited ({worker.proc.returncode}): {worker.output()}"
            )
        if await cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out after {timeout_s:g}s waiting for: {what}")


async def worker_ready(pg: Any) -> bool:
    rows = await pg.run("super", "SELECT details->>'state' FROM assess_heartbeat")
    return bool(rows) and rows[0][0] == "ready"


async def counts(pg: Any) -> dict[str, int]:
    """Row counts only: jobs by state, effects, expired leases, dead."""
    jobs = {
        r[0]: r[1]
        for r in await pg.run(
            "super", "SELECT state, count(*) FROM assess_jobs GROUP BY 1"
        )
    }
    effects = (
        await pg.run(
            "super", "SELECT count(*), count(DISTINCT job_n) FROM assess_chaos_effects"
        )
    )[0]
    expired = (
        await pg.run(
            "super",
            "SELECT count(*) FROM assess_jobs WHERE state='leased' AND lease_until < now()",
        )
    )[0][0]
    return {
        "done": jobs.get("done", 0),
        "ready": jobs.get("ready", 0),
        "leased": jobs.get("leased", 0),
        "dead": jobs.get("dead", 0),
        "effects": effects[0],
        "effects_distinct": effects[1],
        "expired_leases": expired,
    }


async def reset_tables(pg: Any) -> None:
    await pg.run("super", "DELETE FROM assess_jobs")
    await pg.run("super", "DELETE FROM assess_heartbeat")
    await pg.run("super", "DROP TABLE IF EXISTS assess_chaos_effects")
    await pg.run(
        "super",
        "CREATE TABLE assess_chaos_effects (job_n INTEGER PRIMARY KEY, runs INTEGER NOT NULL)",
    )
    await pg.run(
        "super",
        "GRANT SELECT, INSERT, UPDATE ON assess_chaos_effects TO vektra_assess_worker",
    )


async def enqueue(pg: Any, n: int, *, sleep_ms: int) -> None:
    await pg.run(
        "super",
        "INSERT INTO assess_jobs (kind, args) SELECT 'purge', "
        "jsonb_build_object('n', g, 'sleep_ms', CAST(:s AS integer)) FROM generate_series(1, :n) g",
        {"n": n, "s": sleep_ms},
    )

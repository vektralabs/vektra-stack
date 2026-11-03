"""The worker's control connection: ``LISTEN`` plus session-level advisory locks (INT-002).

One persistent asyncpg connection, **outside** the job pool (``application_name =
vektra-assess-worker-control``), so the connection budget stays pool 5 + 1. It does two things:

- ``LISTEN assess_jobs``: a notification only wakes the claim loop sooner; the 1 s poll is the
  correctness backstop, so a missed notification costs latency, never a job;
- holds the ``pg_try_advisory_lock`` of running ticks. Session-level locks die with the session:
  if the connection drops, the server releases every lock and the tick it protected is no longer
  fenced. ``on_lost`` fires so the caller cancels those ticks; ``supervise`` reconnects with
  backoff and re-listens.

Startup runs a self-test: a second connection sends ``NOTIFY`` and the control connection must
see it within 2 s. A transaction-mode pooler (pgbouncer) breaks ``LISTEN`` and session locks
silently, so the self-test turns that into a refusal to start instead of a degraded 1 s poll.

Lock calls are serialised by an ``asyncio.Lock`` (one connection runs one statement at a time).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

from vektra_assess.jobs import NOTIFY_CHANNEL
from vektra_assess.worker.runtime import backoff_s

log = logging.getLogger("vektra_assess.worker")

APPLICATION_NAME = "vektra-assess-worker-control"
_SELFTEST_CHANNEL = "assess_selftest"
# what a dead or half-open control connection raises: a statement timeout, a reset socket, a server
# shutdown, a closed interface. Any of them means the session (and its locks) cannot be trusted.
_LOST = (TimeoutError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError)


class ControlConnectionError(RuntimeError):
    """The control connection cannot do its job; the worker refuses to start."""


class ControlConnection:
    def __init__(
        self,
        dsn: str,
        *,
        on_notify: Callable[[], None],
        on_lost: Callable[[], Awaitable[None] | None] | None = None,
        connect_timeout_s: float = 5.0,
        command_timeout_s: float = 5.0,
        selftest_timeout_s: float = 2.0,
        reconnect_base_s: float = 0.5,
        reconnect_cap_s: float = 10.0,
        rng: random.Random | None = None,
    ) -> None:
        self._dsn = dsn
        self._on_notify = on_notify
        self._on_lost = on_lost
        self._connect_timeout_s = connect_timeout_s
        self._command_timeout_s = command_timeout_s
        self._selftest_timeout_s = selftest_timeout_s
        self._reconnect = (reconnect_base_s, reconnect_cap_s)
        self._rng = rng
        self._conn: asyncpg.Connection | None = None
        self._stmt_lock = asyncio.Lock()
        self._lost = asyncio.Event()
        self._stopping = asyncio.Event()
        self._held: set[int] = set()
        self._bg: set[asyncio.Future[Any]] = set()
        self.reconnects = 0

    # --- lifecycle -------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.is_closed()

    @property
    def held_locks(self) -> frozenset[int]:
        return frozenset(self._held)

    async def start(self) -> None:
        """Connect, LISTEN and run the NOTIFY self-test; raise ``ControlConnectionError``."""
        await self._connect()
        await self._selftest()

    async def stop(self) -> None:
        self._stopping.set()
        self._lost.set()
        conn, self._conn = self._conn, None
        self._held.clear()
        if conn is not None and not conn.is_closed():
            await conn.close(timeout=2)

    async def _connect(self) -> None:
        try:
            conn = await asyncpg.connect(
                self._dsn,
                timeout=self._connect_timeout_s,
                command_timeout=self._command_timeout_s,
                server_settings={"application_name": APPLICATION_NAME},
            )
        except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
            raise ControlConnectionError(
                f"control connection failed: {type(exc).__name__}"
            ) from exc
        await conn.add_listener(NOTIFY_CHANNEL, self._notified)
        conn.add_termination_listener(self._terminated)
        self._conn = conn
        self._lost.clear()

    def _notified(self, *_: Any) -> None:
        self._on_notify()

    def _terminated(self, _conn: asyncpg.Connection) -> None:
        # asyncpg calls this when the connection closes, whatever the cause
        self._mark_lost()

    def _mark_lost(self) -> None:
        """Idempotent: the server released every session lock with the session."""
        if self._stopping.is_set() or self._lost.is_set():
            return
        self._held.clear()
        self._lost.set()
        if self._on_lost is not None:
            result = self._on_lost()
            if asyncio.iscoroutine(result):
                task = asyncio.ensure_future(result)
                self._bg.add(task)
                task.add_done_callback(self._bg.discard)

    def _declare_lost(
        self, conn: asyncpg.Connection, exc: BaseException
    ) -> ControlConnectionError:
        """A statement failed or timed out: the connection may be half-open (no FIN, no error).

        It cannot be trusted, so drop the socket without waiting for it, report the loss, and let
        ``supervise`` reconnect. Returns the error to raise.
        """
        log.warning(
            "assess_control_connection_lost", extra={"error_type": type(exc).__name__}
        )
        with contextlib.suppress(Exception):
            conn.terminate()
        self._mark_lost()
        return ControlConnectionError(f"control connection lost: {type(exc).__name__}")

    # --- self-test -------------------------------------------------------------------------

    async def _send_probe_notify(self) -> None:
        """NOTIFY from a different session: a pooler that maps both to one backend would hide
        the problem, one that does not breaks LISTEN, and either way delivery is the check."""
        other = await asyncpg.connect(self._dsn, timeout=self._connect_timeout_s)
        try:
            await other.execute(f"SELECT pg_notify('{_SELFTEST_CHANNEL}', '')")
        finally:
            await other.close(timeout=2)

    async def _selftest(self) -> None:
        assert self._conn is not None
        seen = asyncio.Event()
        listener = lambda *_: seen.set()  # noqa: E731
        await self._conn.add_listener(_SELFTEST_CHANNEL, listener)
        try:
            await self._send_probe_notify()
            try:
                await asyncio.wait_for(seen.wait(), timeout=self._selftest_timeout_s)
            except TimeoutError:
                raise ControlConnectionError(
                    "LISTEN/NOTIFY self-test failed: no notification within "
                    f"{self._selftest_timeout_s:g} s. Is the worker DSN behind a "
                    "transaction-mode pooler (pgbouncer)? LISTEN and session advisory "
                    "locks need a session-mode or direct connection."
                ) from None
        finally:
            if self.connected:
                await self._conn.remove_listener(_SELFTEST_CHANNEL, listener)

    # --- advisory locks --------------------------------------------------------------------

    async def try_lock(self, key: int) -> bool:
        """``pg_try_advisory_lock``: True when this process now holds ``key``."""
        async with self._stmt_lock:
            conn = self._require()
            try:
                got = bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", key))
            except _LOST as exc:
                raise self._declare_lost(conn, exc) from exc
            if got:
                self._held.add(key)
            return got

    async def unlock(self, key: int) -> None:
        async with self._stmt_lock:
            self._held.discard(key)
            if self.connected:  # a lost connection already released it
                assert self._conn is not None
                conn = self._conn
                try:
                    await conn.fetchval("SELECT pg_advisory_unlock($1)", key)
                except _LOST as exc:
                    raise self._declare_lost(conn, exc) from exc

    def _require(self) -> asyncpg.Connection:
        if not self.connected:
            raise ControlConnectionError("control connection is down")
        assert self._conn is not None
        return self._conn

    # --- reconnect -------------------------------------------------------------------------

    async def supervise(
        self, on_reconnected: Callable[[], Awaitable[None]] | None = None
    ) -> None:
        """Reconnect with backoff whenever the connection is lost, until ``stop()``."""
        while not self._stopping.is_set():
            await self._lost.wait()
            if self._stopping.is_set():
                return
            attempt = 0
            while not self._stopping.is_set():
                attempt += 1
                try:
                    await self._connect()
                except ControlConnectionError as exc:
                    log.warning(
                        "assess_control_reconnect_failed",
                        extra={"attempt": attempt, "error": str(exc)},
                    )
                    await asyncio.sleep(
                        backoff_s(
                            attempt,
                            base_s=self._reconnect[0],
                            cap_s=self._reconnect[1],
                            rng=self._rng,
                        )
                    )
                    continue
                self.reconnects += 1
                log.warning("assess_control_reconnected", extra={"attempt": attempt})
                self._on_notify()  # anything queued while we were deaf: poll now
                if on_reconnected is not None:
                    await on_reconnected()
                break

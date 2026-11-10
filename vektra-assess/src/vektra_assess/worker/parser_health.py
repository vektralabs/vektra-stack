"""Parser sidecar status for ``/health/assess`` (plan assess-privacy-retention 12b, COMP-002).

The API cannot reach the parser (another container, a socket only the worker mounts), so the
worker's ``parser_ping`` tick pings it and keeps the outcome here; the heartbeat publishes it in
the worker row as ``parser: {"ok", "at", "error"}`` (an error CLASS name, never a message), and
``/health/assess`` reads it from there. A worker that never pinged publishes nothing: the
readiness reports ``unknown`` rather than inventing a status.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from vektra_assess.worker.parser_client import ParserUnavailableError


class _Pinger(Protocol):
    def ping(self, deep: bool = False) -> Awaitable[float]: ...


class ParserHealth:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self.ok: bool | None = None
        self.at: float | None = None
        self.error: str | None = None

    async def check(self, client: _Pinger) -> None:
        try:
            await client.ping()
        except ParserUnavailableError as exc:
            self.ok, self.error = False, (str(exc) or "unavailable")[:40]
        except Exception as exc:
            self.ok, self.error = False, type(exc).__name__
        else:
            self.ok, self.error = True, None
        self.at = self._clock()

    def stats(self) -> dict[str, Any]:
        if self.ok is None or self.at is None:
            return {}
        return {"parser": {"ok": self.ok, "at": round(self.at, 1), "error": self.error}}

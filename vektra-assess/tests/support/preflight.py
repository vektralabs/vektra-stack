"""Pre-flight test parts: a fake Part B runner, fake start sources and a job context factory."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.capture.capture import CaptureResult
from vektra_assess.jobs import JobKind
from vektra_assess.keys import AssessKeyring
from vektra_assess.llm.types import ProviderCallRecord, TurnResult, TurnUsage
from vektra_assess.worker.preflight import PreflightDeps, StartSource, StartTarget
from vektra_assess.worker.runner import JobContext

PART_B_TEXT = "Partiamo dal criterio C1: come hai scelto la struttura della relazione?"
MODEL = "fake/part-b"
PROMPT_VERSION = "0a1b2c3d"


class FakePartB:
    """``run_part_b`` without a model: a fixed orientation, or a scripted failure / delay."""

    def __init__(
        self,
        text: str = PART_B_TEXT,
        *,
        fail: BaseException | None = None,
        delay_s: float = 0.0,
        release: asyncio.Event | None = None,
    ) -> None:
        self.text = text
        self.fail = fail
        self.delay_s = delay_s
        self.release = release  # a stalled provider: waits until the test sets it
        self.started = asyncio.Event()
        self.calls: list[float | None] = []

    @property
    def model(self) -> str:
        return MODEL

    @property
    def prompt_version(self) -> str:
        return PROMPT_VERSION

    async def run_part_b(
        self,
        bundle: ContextBundle,
        *,
        language: str = "it",
        remaining_s: float | None = None,
    ) -> TurnResult:
        self.calls.append(remaining_s)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail is not None:
            raise self.fail
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        return TurnResult(
            text=self.text,
            marks=(),
            behaviour_class=None,
            contest_entries=(),
            language=language,
            usage=TurnUsage(120, 30),
            call_records=(ProviderCallRecord(now, now, now, "ok"),),
            replaced_count=0,
            content_hash=hashlib.sha256(self.text.encode()).hexdigest(),
        )


class FakeSource:
    """A ``StartSource`` that returns a fixed result, raises, or waits forever."""

    def __init__(
        self,
        result: CaptureResult | None = None,
        *,
        raises: BaseException | None = None,
        stall: bool = False,
        inner: StartSource | None = None,
    ) -> None:
        self.result = result
        self.raises = raises
        self.stall = stall
        self.inner = inner
        self.targets: list[StartTarget] = []

    async def capture(self, target: StartTarget) -> CaptureResult:
        self.targets.append(target)
        if self.stall:
            await asyncio.Event().wait()
        if self.raises is not None:
            raise self.raises
        if self.inner is not None:
            return await self.inner.capture(target)
        assert self.result is not None
        return self.result


def kek_ring() -> AssessKeyring:
    return AssessKeyring("assess_kek", "kek1", {"kek1": os.urandom(32)})


def deps(
    settings: VektraAssessSettings,
    handoff_public: AssessKeyring,
    source: StartSource,
    runner: Any | None = None,
    kek: AssessKeyring | None = None,
) -> PreflightDeps:
    return PreflightDeps(
        settings=settings,
        kek=kek or kek_ring(),
        handoff_public=handoff_public,
        source=source,
        runner=runner or FakePartB(),
    )


def job_context(engine: AsyncEngine, session_id: uuid.UUID) -> JobContext:
    return JobContext(
        job_id=1,
        kind=JobKind.PREFLIGHT,
        args={"session_id": str(session_id)},
        attempt=1,
        acquire=engine.connect,
        log=logging.getLogger("test"),
    )


async def activate(pg: Any, model: str = MODEL, pv: str = PROMPT_VERSION) -> None:
    await pg.run(
        "super",
        "INSERT INTO assess_model_activations (provider, model, prompt_version, "
        "report_sha256, hard_pass) VALUES ('fake', :m, :p, :s, true)",
        {"m": model, "p": pv, "s": "a" * 64},
    )


Runner = Callable[[JobContext], Awaitable[None]]


class FakeStreamRunner:
    """``run_stream`` without a model: scripted released segments, then one ``TurnResult``.

    ``release`` stalls the stream before its first segment until the test sets it; ``fail`` raises
    (for example a ``ProviderOutageError``) instead of answering.
    """

    def __init__(
        self,
        segments: tuple[str, ...] = (
            "Bene. ",
            "Quale scelta del criterio C1 vuoi spiegare?",
        ),
        *,
        release: asyncio.Event | None = None,
        fail: BaseException | None = None,
    ) -> None:
        self.segments = segments
        self.release = release
        self.fail = fail
        self.turns: list[Any] = []

    async def run_stream(
        self, bundle: ContextBundle, turn: Any, stop: Any = None
    ) -> Any:
        from datetime import UTC, datetime

        from vektra_assess.llm.types import ReleasedSegment

        self.turns.append(turn)
        if self.release is not None:
            await self.release.wait()
        if self.fail is not None:
            raise self.fail
        for seg in self.segments:
            yield ReleasedSegment(seg)
        text = "".join(self.segments)
        now = datetime.now(UTC)
        yield TurnResult(
            text=text,
            marks=(),
            behaviour_class=None,
            contest_entries=(),
            language="it",
            usage=TurnUsage(200, 20),
            call_records=(ProviderCallRecord(now, now, now, "ok"),),
            replaced_count=0,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
        )

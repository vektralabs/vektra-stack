"""Composition of the discussion runner (plan assess-llm-runner, task C; ARCH-074).

``vektra_assess.llm`` imports no other component: the LLM provider, the safeguard, the trace
writer and the audit sink are built here and injected.

- ``build_assess_safeguard``: FAIL-CLOSED. vektra_core's ``create_safeguard`` falls open to a
  PassthroughSafeguard when Presidio cannot load; for assess that is a startup error. ``none`` is
  honoured only when ``VEKTRA_ASSESS_SAFEGUARD_MODE=none`` says so explicitly.
- ``build_assess_llm``: a dedicated LitellmProvider for ``VEKTRA_ASSESS_LLM_MODEL`` with NO
  fallback model, whatever ``VEKTRA_LLM_FALLBACK_MODEL`` says: a fallback would answer students
  with a (provider, model) that never passed the NFR-023 activation gate.
- ``AnalyticsTraceWriter``: adapts the runner's ``TraceWriter`` to ``AnalyticsService.store_trace``
  in its own session and transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vektra_assess.config import VektraAssessSettings
from vektra_assess.llm import DiscussionTurnRunner
from vektra_assess.llm.types import AuditSink, TraceWriter
from vektra_shared.protocols import LLMProvider, SafeguardHook
from vektra_shared.safeguards import PassthroughSafeguard
from vektra_shared.startup import StartupValidationError
from vektra_shared.types import QueryTrace

STEP = "assess_llm_check"


def build_assess_safeguard(
    mode: str, *, factory: Callable[[str], SafeguardHook] | None = None
) -> SafeguardHook:
    if mode == "none":
        return PassthroughSafeguard()
    if factory is None:
        from vektra_core.safeguards import create_safeguard

        factory = create_safeguard
    hook = factory(mode)
    if isinstance(hook, PassthroughSafeguard):
        raise StartupValidationError(
            step=STEP,
            detail=f"VEKTRA_ASSESS_SAFEGUARD_MODE={mode} but the safeguard fell back to passthrough",
            remediation=(
                "Install the Presidio extra (INSTALL_PRESIDIO) or set "
                "VEKTRA_ASSESS_SAFEGUARD_MODE=none explicitly."
            ),
        )
    return hook


def build_assess_llm(settings: VektraAssessSettings) -> LLMProvider:
    from vektra_core.providers.litellm_provider import LitellmProvider
    from vektra_shared.config import LLMConfig

    if not settings.llm_model:
        raise StartupValidationError(
            step=STEP,
            detail="VEKTRA_ASSESS_ENABLED=true but VEKTRA_ASSESS_LLM_MODEL is unset",
            remediation="Set the dedicated discussion model (litellm id).",
        )
    # init values beat the environment: the RAG fallback model must not leak in
    config = LLMConfig(
        VEKTRA_LLM_PROVIDER=settings.llm_model,
        VEKTRA_LLM_API_BASE=settings.llm_api_base,
        VEKTRA_LLM_API_KEY=None,
        VEKTRA_LLM_EXTRA_BODY=None,
        VEKTRA_LLM_FALLBACK_MODEL=None,
    )
    return LitellmProvider(config)


class AnalyticsTraceWriter:
    """``TraceWriter`` over ``AnalyticsService.store_trace``, one transaction per trace."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def write(self, trace: QueryTrace, namespace: str) -> None:
        from vektra_analytics.service import AnalyticsService

        async with self._session_factory() as session:
            await AnalyticsService().store_trace(session, trace, namespace)
            await session.commit()


def build_assess_runner(
    settings: VektraAssessSettings,
    *,
    safeguard: SafeguardHook,
    trace_writer: TraceWriter,
    audit: AuditSink,
    llm: LLMProvider | None = None,
) -> DiscussionTurnRunner:
    return DiscussionTurnRunner(
        llm=llm or build_assess_llm(settings),
        safeguard=safeguard,
        trace_writer=trace_writer,
        settings=settings,
        audit=audit,
    )

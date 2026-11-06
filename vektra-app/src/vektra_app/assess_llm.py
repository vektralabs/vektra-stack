"""Composition of the discussion runner (plan assess-llm-runner, task C; ARCH-074).

``vektra_assess.llm`` imports no other component: the LLM provider, the safeguard, the trace
writer and the audit sink are built here and injected.

- ``build_assess_safeguard``: FAIL-CLOSED. vektra_core's ``create_safeguard`` falls open to a
  PassthroughSafeguard when Presidio cannot load; for assess that is a startup error. ``none`` is
  honoured only when ``VEKTRA_ASSESS_SAFEGUARD_MODE=none`` says so explicitly.
- ``build_assess_llm``: a dedicated LitellmProvider for ``VEKTRA_ASSESS_LLM_MODEL`` with NO
  fallback model, whatever ``VEKTRA_LLM_FALLBACK_MODEL`` says: a fallback would answer students
  with a (provider, model) that never passed the NFR-023 activation gate. Its key is the shared
  ``VEKTRA_LLM_API_KEY``; its extra body is ``VEKTRA_ASSESS_LLM_EXTRA_BODY``, never the RAG's.
- ``AnalyticsTraceWriter``: adapts the runner's ``TraceWriter`` to ``AnalyticsService.store_trace``
  in its own session and transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vektra_assess.config import VektraAssessSettings
from vektra_assess.llm import DiscussionTurnRunner
from vektra_assess.llm.types import AuditSink, RunnerMetrics, TraceWriter
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
    # vektra_core's Presidio safeguard loads its engines lazily and falls open to "allowed" on
    # a missing package or spaCy model, at the first call; load them NOW so that failure stops
    # startup instead of shipping every turn unfiltered
    warm = getattr(hook, "_ensure_engines", None)
    if callable(warm) and not warm():
        raise StartupValidationError(
            step=STEP,
            detail=f"VEKTRA_ASSESS_SAFEGUARD_MODE={mode} but its engines failed to load",
            remediation=(
                "Install the Presidio extra and a spaCy model (en_core_web_lg or _sm), or set "
                "VEKTRA_ASSESS_SAFEGUARD_MODE=none explicitly."
            ),
        )
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
    # Init values beat the environment: the RAG fallback model and the RAG extra body must not leak
    # in. The API key IS the RAG's VEKTRA_LLM_API_KEY (one key for the on-prem vLLM, decision of
    # 2026-11-06), read through the assess settings so the worker, which has no .env, gets it from
    # its own environment.
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
    config = LLMConfig(
        VEKTRA_LLM_PROVIDER=settings.llm_model,
        VEKTRA_LLM_API_BASE=settings.llm_api_base,
        VEKTRA_LLM_API_KEY=key,
        VEKTRA_LLM_EXTRA_BODY=None,
        VEKTRA_LLM_FALLBACK_MODEL=None,
    )
    # Set AFTER construction: pydantic-settings deep-merges a dict init value with the environment,
    # so passing it above would add the RAG's VEKTRA_LLM_EXTRA_BODY keys to the assess body.
    config = config.model_copy(update={"extra_body": settings.llm_extra_body})
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


class PrometheusRunnerMetrics:
    """``RunnerMetrics`` over prometheus_client (API process). Labels are codes, never content:
    a post-check stage, a behaviour class, a call outcome, a Part B rejection code."""

    _counters: dict[str, Any] = {}

    def __init__(self) -> None:
        from prometheus_client import Counter

        c = PrometheusRunnerMetrics._counters
        if (
            not c
        ):  # registered once per process (the default registry refuses duplicates)
            c["replacement"] = Counter(
                "vektra_assess_postcheck_replacements_total",
                "Post-check replacements and removals in delivered turns, by stage",
                ["stage"],
            )
            c["class"] = Counter(
                "vektra_assess_behaviour_class_total",
                "Turns answered with a behaviour-class core sentence, by class",
                ["behaviour_class"],
            )
            c["outcome"] = Counter(
                "vektra_assess_provider_calls_total",
                "Discussion-model calls by outcome (ok, error, first_token_timeout, cancelled)",
                ["outcome"],
            )
            c["part_b"] = Counter(
                "vektra_assess_part_b_rejections_total",
                "Part B generations rejected, by content-free rejection code",
                ["code"],
            )
        self._c = c

    def replacement(self, stage: str, count: int) -> None:
        self._c["replacement"].labels(stage=stage).inc(count)

    def behaviour_class(self, code: str) -> None:
        self._c["class"].labels(behaviour_class=code).inc()

    def call_outcome(self, outcome: str) -> None:
        self._c["outcome"].labels(outcome=outcome).inc()

    def part_b_rejected(self, code: str) -> None:
        self._c["part_b"].labels(code=code).inc()


def build_assess_runner(
    settings: VektraAssessSettings,
    *,
    safeguard: SafeguardHook,
    trace_writer: TraceWriter,
    audit: AuditSink,
    llm: LLMProvider | None = None,
    metrics: RunnerMetrics | None = None,
) -> DiscussionTurnRunner:
    return DiscussionTurnRunner(
        llm=llm or build_assess_llm(settings),
        safeguard=safeguard,
        trace_writer=trace_writer,
        settings=settings,
        audit=audit,
        metrics=metrics,
    )


# --- worker (Part B) ----------------------------------------------------------------------------

_INSERT_TRACE = (
    "INSERT INTO query_traces (response_id, namespace_id, steps, total_duration_ms, "
    "chunks_retrieved, llm_model, prompt_version, created_at, pipeline) VALUES (:response_id, "
    ":namespace_id, CAST(:steps AS jsonb), :total_duration_ms, CAST(:chunks AS jsonb), "
    ":llm_model, :prompt_version, :created_at, :pipeline)"
)


class WorkerTraceWriter:
    """``TraceWriter`` for the assess worker: raw SQL as ``vektra_assess_worker``.

    The worker does not import vektra_analytics. Its role holds INSERT on the trace columns only
    (revision 0020), and a RESTRICTIVE policy limits those rows to the assess namespace and
    pipeline, so a trace aimed anywhere else is refused by the database, not by this code.
    """

    def __init__(self, acquire_db: Callable[[], Any]) -> None:
        self._acquire_db = acquire_db

    async def write(self, trace: QueryTrace, namespace: str) -> None:
        import json

        from sqlalchemy import text

        async with self._acquire_db() as conn:
            await conn.execute(
                text(_INSERT_TRACE),
                {
                    "response_id": trace.response_id,
                    "namespace_id": namespace,
                    "steps": json.dumps(
                        [
                            {
                                "name": s.name,
                                "duration_ms": s.duration_ms,
                                "metadata": s.metadata,
                            }
                            for s in trace.steps
                        ]
                    ),
                    "total_duration_ms": trace.total_duration_ms,
                    "chunks": json.dumps([]),
                    "llm_model": trace.llm_model,
                    "prompt_version": trace.prompt_version,
                    "created_at": trace.created_at,
                    "pipeline": trace.pipeline,
                },
            )
            await conn.commit()


def build_worker_part_b_runner(
    settings: VektraAssessSettings,
    *,
    acquire_db: Callable[[], Any],
    audit: AuditSink,
    llm: LLMProvider | None = None,
) -> DiscussionTurnRunner:
    """The runner the worker uses for Part B (plan 8 pre-flight).

    Its safeguard is an EXPLICIT passthrough, decided on 2026-11-05 (plan Notes): the worker's
    import ceiling (ARCH-073) excludes Presidio and spaCy, Part B has no student-typed input (the
    bundle only), and its output is rejected by the deterministic Part B checks (values, words,
    labels, lexicon, tags, quotes). This is not the fail-open fallback ``build_assess_safeguard``
    refuses: it is the worker's declared configuration, and the API keeps the fail-closed one.
    """
    return build_assess_runner(
        settings,
        safeguard=PassthroughSafeguard(),
        trace_writer=WorkerTraceWriter(acquire_db),
        audit=audit,
        llm=llm,
    )


# --- startup ------------------------------------------------------------------------------------


async def _check_activation(
    app: Any, settings: VektraAssessSettings, prompt_version: str
) -> bool:
    """NFR-023: is the (model, prompt_version) triple activated? Never refuses startup.

    The activation is recorded through this API (``POST /api/v1/assess/admin/model-activations``),
    so refusing to start without one would leave a first deployment, and every deployment after a
    ``prompt_version`` change, with no way to activate. The API therefore starts DEGRADED: the
    missing activation is logged as an error with its remediation and kept on ``app.state``; no
    discussion is served, because the session pre-flight (plan 8) checks ``is_activated`` against
    the table before every start (fail closed), and a new row takes effect without a restart.
    """
    import structlog

    from vektra_assess.llm.activation import is_activated

    log = structlog.get_logger(__name__)
    engine = getattr(app.state, "assess_engine", None)
    activated = False
    if engine is not None:
        async with engine.connect() as conn:
            activated = await is_activated(
                conn, str(settings.llm_model), prompt_version
            )
    if activated:
        return True
    if settings.llm_allow_unactivated:  # development only, checked above
        log.warning(
            "assess_llm_unactivated_allowed", step=STEP, prompt_version=prompt_version
        )
        return True
    log.error(
        "assess_llm_unactivated",
        step=STEP,
        model=str(settings.llm_model),
        prompt_version=prompt_version,
        detail="no passing NFR-023 activation: discussions are refused until one is recorded",
        remediation=(
            "Run make eval-assess against this model and POST the PASS report to "
            "/api/v1/assess/admin/model-activations. A changed template, shipped string or "
            "lexicon is a new prompt_version and needs a new activation."
        ),
    )
    return False


async def step_13_assess_llm_check(
    app: Any, *, safeguard_factory: Callable[[str], SafeguardHook] | None = None
) -> None:
    """ARCH-057 step 13 (plan assess-llm-runner): the discussion runner's parts, fail closed.

    Disabled: nothing is built (dormant). Enabled: the safeguard must load in the configured mode
    (Presidio falling open is refused), ``VEKTRA_ASSESS_LLM_MODEL`` must be set, and the parts are
    kept on ``app.state`` for the turn route (plan 8), which adds its own audit sink per request.
    A missing NFR-023 activation does NOT stop startup (the activation endpoint lives in this
    API): it is logged and recorded as ``app.state.assess_llm_activated``, and the session
    pre-flight refuses every discussion until a passing row exists.
    """
    import structlog

    from vektra_app.assess_wiring import load_assess_settings
    from vektra_assess.llm.postcheck.lexicon import LEXICON_VERSION
    from vektra_assess.llm.prompts import PromptRenderer

    log = structlog.get_logger(__name__)
    settings = load_assess_settings()
    if not settings.enabled:
        log.info("startup_step", step=STEP, status="disabled")
        return
    if settings.llm_allow_unactivated and settings.env != "development":
        raise StartupValidationError(
            step=STEP,
            detail=(
                "VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED is honoured only with VEKTRA_ENV=development "
                f"(this process runs with VEKTRA_ENV={settings.env})"
            ),
            remediation="Unset it, and activate the model through the NFR-023 endpoint.",
        )
    app.state.assess_safeguard = build_assess_safeguard(
        settings.safeguard_mode, factory=safeguard_factory
    )
    app.state.assess_llm = build_assess_llm(settings)
    app.state.assess_metrics = (
        PrometheusRunnerMetrics()
    )  # served on /metrics with the rest
    app.state.assess_prompt_version = PromptRenderer(LEXICON_VERSION).prompt_version
    # the turn route's runner (plan assess-session-lifecycle M1.9): its audit sink writes through
    # the assess API engine (step 12), its traces through analytics, in their own transactions
    from vektra_assess.api.turns import ApiAuditSink
    from vektra_shared.db import get_session_factory

    app.state.assess_runner = build_assess_runner(
        settings,
        safeguard=app.state.assess_safeguard,
        # resolved per write: the main database is initialised by an earlier startup step
        trace_writer=AnalyticsTraceWriter(lambda: get_session_factory()()),
        audit=ApiAuditSink(lambda: app.state.assess_engine),
        llm=app.state.assess_llm,
        metrics=app.state.assess_metrics,
    )
    app.state.assess_llm_activated = await _check_activation(
        app, settings, app.state.assess_prompt_version
    )
    log.info(
        "startup_step",
        step=STEP,
        status="ok" if app.state.assess_llm_activated else "degraded",
        safeguard=settings.safeguard_mode,
        prompt_version=app.state.assess_prompt_version,
    )

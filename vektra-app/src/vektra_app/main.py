"""Vektra application entrypoint (ARCH-001, ARCH-015, ARCH-057).

Assembles all component routers, middleware, and providers into a single
FastAPI application. Runs the startup validation sequence (11 steps in
Phase 2, extended from the original 8).

This is the ONLY module that imports from all vektra_* components.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncGenerator, MutableMapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from vektra_app import __version__
from vektra_shared.config import QueryPipelineConfig, VektraSettings
from vektra_shared.db import init_db
from vektra_shared.errors import ERR_CONFIG_001, ErrorCategory, ErrorResponse
from vektra_shared.registry import ProviderRegistry
from vektra_shared.startup import (
    StartupValidationError,
    check_database_connectivity,
    check_database_schema,
)

log = structlog.get_logger(__name__)


def _redact_url(url: str) -> str:
    """Return scheme://hostname:port only, stripping credentials and path."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


# ---------------------------------------------------------------------------
# Structlog configuration (ARCH-013)
# ---------------------------------------------------------------------------


def _pii_redactor(
    _logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Strip PII-sensitive fields from WARNING+ log events."""
    if method_name in ("warning", "error", "critical"):
        for key in ("query", "question", "answer", "response_text"):
            if key in event_dict:
                event_dict[key] = "[REDACTED]"
    return event_dict


def configure_structlog() -> None:
    """Configure structlog with JSON output, timestamps, and PII redaction."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _pii_redactor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# ARCH-057: 11-step startup validation (Phase 2)
# ---------------------------------------------------------------------------


async def _step_4_pgvector_check() -> None:
    """ARCH-057 step 4: verify pgvector extension is installed."""
    from sqlalchemy import text

    from vektra_shared.db import get_engine

    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise StartupValidationError(
                step="pgvector_extension",
                detail="pgvector extension is not installed in the database.",
                remediation=(
                    "Install pgvector: CREATE EXTENSION IF NOT EXISTS vector; "
                    "Or use the official pgvector Docker image."
                ),
            )


async def _step_5_register_providers(
    settings: VektraSettings, registry: ProviderRegistry
) -> None:
    """ARCH-057 step 5: instantiate and register all providers."""
    # --- LLM ---
    from vektra_core.providers.litellm_provider import LitellmProvider

    llm_config = settings.as_llm_config()
    llm_provider = LitellmProvider(config=llm_config)
    registry.register("llm", "default", llm_provider)

    # --- Embedding ---
    from vektra_index.providers.sentence_transformers import (
        SentenceTransformersProvider,
    )

    embedding_provider = SentenceTransformersProvider(
        model_name=settings.embedding_model
    )
    registry.register("embedding", "default", embedding_provider)
    registry.register("embedding", "sentence-transformers", embedding_provider)

    # --- Vector store ---
    from vektra_index.adapters import VectorStoreServiceAdapter

    vector_store_adapter = VectorStoreServiceAdapter(
        active_index_version=settings.active_index_version
    )
    registry.register("vector_store", "default", vector_store_adapter)
    registry.register("vector_store", "pgvector", vector_store_adapter)

    # --- Sparse embedding (Phase 2, conditional) ---
    sparse_provider = None
    if settings.sparse_embedding_provider:
        from vektra_index.providers.fastembed_bm25 import FastEmbedBM25Provider

        model_name = settings.sparse_embedding_model or "Qdrant/bm25"
        sparse_provider = FastEmbedBM25Provider(model_name=model_name)
        registry.register("sparse_embedding", "default", sparse_provider)
        log.info(
            "sparse_embedding_registered",
            provider=settings.sparse_embedding_provider,
            model=model_name,
        )

    # --- Qdrant vector store (Phase 2, conditional) ---
    if settings.vector_store_provider == "qdrant":
        from vektra_index.providers.qdrant import QdrantVectorStoreProvider

        qdrant_provider = QdrantVectorStoreProvider(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            collection_name=settings.qdrant_collection,
            active_index_version=settings.active_index_version,
        )
        registry.register("vector_store", "default", qdrant_provider)
        registry.register("vector_store", "qdrant", qdrant_provider)
        # Override pgvector adapter for health checks
        vector_store_adapter = qdrant_provider  # type: ignore[assignment]
        log.info(
            "vector_store_registered",
            provider="qdrant",
            url=_redact_url(settings.qdrant_url),
        )

    # --- Safeguard (Phase 2: configurable mode) ---
    from vektra_core.safeguards import create_safeguard

    safeguard = create_safeguard(
        settings.safeguard_mode,
        pii_chunk_threshold=settings.pii_chunk_threshold,
    )
    registry.register("safeguard", "default", safeguard)

    # --- Event emitter ---
    from vektra_shared.config import WebhookConfig
    from vektra_shared.events import NoOpEventEmitter, WebhookEventEmitter

    webhook_config = WebhookConfig()
    if webhook_config.url:
        event_emitter = WebhookEventEmitter(config=webhook_config)
        log.info(
            "event_emitter_registered",
            type="webhook",
            url=_redact_url(webhook_config.url),
        )
    else:
        event_emitter = NoOpEventEmitter()
    registry.register("event_emitter", "default", event_emitter)
    registry.register("events", "default", event_emitter)

    # --- Key store ---
    from vektra_admin.keystore import InMemoryKeyStore
    from vektra_shared.db import get_session_factory

    key_store = InMemoryKeyStore()
    async with get_session_factory()() as session:
        await key_store.load_from_db(session)
    registry.register("key_store", "default", key_store)

    # --- Conversation store ---
    from vektra_core.conversation import (
        InMemoryConversationStore,
        PersistentConversationStore,
    )
    from vektra_core.templates import TemplateRenderer

    conversation_store: InMemoryConversationStore | PersistentConversationStore
    if settings.conversation_key:
        conversation_store = PersistentConversationStore(
            session_factory=get_session_factory(),
            encryption_key=settings.conversation_key,
            max_turns=settings.max_conversation_turns,
        )
        log.info("conversation_store_selected", backend="persistent")
    else:
        conversation_store = InMemoryConversationStore(
            max_turns=settings.max_conversation_turns,
        )
        log.warning(
            "conversation_store_selected",
            backend="in_memory",
            persistence="disabled",
        )

    registry.register("conversation_store", "default", conversation_store)

    # --- Reranker (Phase 2, conditional) ---
    from vektra_core.reranker import create_reranker

    pipeline_config = QueryPipelineConfig()
    reranker = create_reranker(pipeline_config.rerank)

    # --- Query pipeline (Phase 2: select simple or advanced) ---
    templates_dir = (
        Path(settings.prompt_templates_dir) if settings.prompt_templates_dir else None
    )
    renderer = TemplateRenderer(templates_dir=templates_dir)

    if settings.query_pipeline == "advanced":
        from vektra_core.advanced_pipeline import AdvancedQueryPipeline

        pipeline = AdvancedQueryPipeline(
            embedding=embedding_provider,
            vector_store=vector_store_adapter,
            llm=llm_provider,
            llm_config=llm_config,
            safeguard=safeguard,
            conversation_store=conversation_store,
            renderer=renderer,
            pipeline_config=pipeline_config,
            sparse_embedding=sparse_provider,
            reranker=reranker,
        )
        log.info("query_pipeline_selected", pipeline="advanced")
    else:
        from vektra_core.pipeline import SimpleQueryPipeline

        pipeline = SimpleQueryPipeline(
            embedding=embedding_provider,
            vector_store=vector_store_adapter,
            llm=llm_provider,
            llm_config=llm_config,
            safeguard=safeguard,
            conversation_store=conversation_store,
            renderer=renderer,
            pipeline_config=pipeline_config,
        )
        log.info("query_pipeline_selected", pipeline="simple")
    registry.register("query_pipeline", "default", pipeline)

    # --- Analytics service ---
    from vektra_analytics.service import AnalyticsService

    analytics_service = AnalyticsService()
    registry.register("analytics", "default", analytics_service)

    # --- Learn service (conditional) ---
    if settings.learn_jwt_secret:
        from vektra_learn.service import LearnService

        learn_service = LearnService(jwt_secret=settings.learn_jwt_secret)
        registry.register("learn", "default", learn_service)
        log.info("learn_service_registered")

    # --- Ingest pipeline (cross-module access via registry) ---
    from vektra_ingest.pipeline import run_ingest

    registry.register("ingest", "default", run_ingest)

    # --- Health checks ---
    registry.register("health", "llm", llm_provider.health_check)
    registry.register("health", "embedding", embedding_provider.health_check)
    registry.register("health", "vector_store", vector_store_adapter.health_check)

    # --- Audit log injection (ADR-0005 safe) ---
    import vektra_shared.audit
    from vektra_admin.audit import log_event as _audit_impl

    vektra_shared.audit.set_log_fn(_audit_impl)


async def _step_6_embedding_warmup(registry: ProviderRegistry) -> None:
    """ARCH-057 step 6: warm up the embedding model."""
    from vektra_index.startup import check_embedding_model

    await check_embedding_model(registry)


async def _step_7_llm_check(
    settings: VektraSettings, registry: ProviderRegistry
) -> None:
    """ARCH-057 step 7: LLM connectivity check (warning-only, not fatal)."""
    if not settings.startup_llm_check:
        log.info("startup_step", step="llm_connectivity", status="skipped")
        return

    try:
        llm_provider = registry.get("llm", "default")
        health = await llm_provider.health_check()
        if health.status != "healthy":
            log.warning(
                "startup_step",
                step="llm_connectivity",
                status="degraded",
                message=health.message,
            )
        else:
            log.info("startup_step", step="llm_connectivity", status="ok")
    except Exception as exc:
        log.warning(
            "startup_step",
            step="llm_connectivity",
            status="warning",
            error=str(exc),
        )


async def _step_8_template_check(settings: VektraSettings) -> None:
    """ARCH-057 step 8: verify Jinja2 prompt templates are loadable."""
    from vektra_core.templates import TemplateRenderer

    templates_dir = (
        Path(settings.prompt_templates_dir) if settings.prompt_templates_dir else None
    )
    try:
        TemplateRenderer(templates_dir=templates_dir)
    except Exception as exc:
        raise StartupValidationError(
            step="template_loading",
            detail=str(exc),
            remediation=(
                "Ensure prompt template files (system.j2, context.j2, conversation.j2) "
                "exist in VEKTRA_PROMPT_TEMPLATES_DIR, "
                "or leave it unset to use built-in defaults."
            ),
        ) from exc


async def _step_9_analytics_check(registry: ProviderRegistry) -> None:
    """ARCH-057 step 9 (Phase 2): verify analytics service is registered."""
    if not registry.has("analytics", "default"):
        raise StartupValidationError(
            step="analytics_check",
            detail="AnalyticsService not registered in ProviderRegistry.",
            remediation="This is a bug in the startup sequence. File an issue.",
        )
    log.info("startup_step", step="analytics_check", status="ok")


async def _step_10_learn_check(
    settings: VektraSettings, registry: ProviderRegistry
) -> None:
    """ARCH-057 step 10 (Phase 2): verify learn JWT secret when learn is active."""
    if not settings.learn_jwt_secret:
        log.info("startup_step", step="learn_check", status="skipped")
        return

    if not registry.has("learn", "default"):
        raise StartupValidationError(
            step="learn_check",
            detail="VEKTRA_LEARN_JWT_SECRET is set but LearnService failed to register.",
            remediation="Check logs for LearnService initialization errors.",
        )
    if len(settings.learn_jwt_secret) < 32:
        raise StartupValidationError(
            step="learn_check",
            detail="VEKTRA_LEARN_JWT_SECRET must be at least 32 characters.",
            remediation="Set a longer JWT secret for production security.",
        )
    log.info("startup_step", step="learn_check", status="ok")


async def _step_11_qdrant_check(
    settings: VektraSettings, registry: ProviderRegistry
) -> None:
    """ARCH-057 step 11 (Phase 2): Qdrant connectivity check (conditional)."""
    if settings.vector_store_provider != "qdrant":
        log.info("startup_step", step="qdrant_check", status="skipped")
        return

    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.qdrant_url}/healthz")
            if resp.status_code == 200:
                # Ensure collection exists on startup
                provider = registry.get("vector_store", "qdrant")
                if hasattr(provider, "ensure_collection"):
                    await provider.ensure_collection()
                log.info("startup_step", step="qdrant_check", status="ok")
            else:
                log.warning(
                    "startup_step",
                    step="qdrant_check",
                    status="degraded",
                    http_status=resp.status_code,
                )
    except Exception as exc:
        log.warning(
            "startup_step",
            step="qdrant_check",
            status="warning",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Lifespan: startup + shutdown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Run the 11-step startup validation, then yield for serving."""
    start_time = time.monotonic()
    configure_structlog()

    # Step 1: Pydantic config validation
    try:
        settings = VektraSettings()
    except ValidationError as exc:
        err = StartupValidationError(
            step="config_validation",
            detail=str(exc),
            remediation=(
                "Set all required environment variables. At minimum: "
                "VEKTRA_LLM_PROVIDER (e.g., 'ollama/llama3' or 'openai/gpt-4o')."
            ),
        )
        log.error("startup_failed", error=err.to_plain_text())
        raise SystemExit(1) from exc

    # Initialize database engine (needed by steps 2-5)
    init_db(settings.database_url)

    registry = ProviderRegistry()

    # Steps 2-8 (step 1 already done above)
    steps: list[tuple[str, Any]] = [
        (
            "database_connectivity",
            lambda: check_database_connectivity(settings.database_url),
        ),
        ("database_schema", lambda: check_database_schema(settings.database_url)),
        ("pgvector_extension", lambda: _step_4_pgvector_check()),
        (
            "provider_registration",
            lambda: _step_5_register_providers(settings, registry),
        ),
        ("embedding_warmup", lambda: _step_6_embedding_warmup(registry)),
        ("llm_connectivity", lambda: _step_7_llm_check(settings, registry)),
        ("template_loading", lambda: _step_8_template_check(settings)),
        ("analytics_check", lambda: _step_9_analytics_check(registry)),
        ("learn_check", lambda: _step_10_learn_check(settings, registry)),
        ("qdrant_check", lambda: _step_11_qdrant_check(settings, registry)),
    ]

    step_1_ms = int((time.monotonic() - start_time) * 1000)
    log.info("startup_step_complete", step="config_validation", duration_ms=step_1_ms)

    for step_name, step_fn in steps:
        step_start = time.monotonic()
        try:
            await step_fn()
            duration_ms = int((time.monotonic() - step_start) * 1000)
            log.info("startup_step_complete", step=step_name, duration_ms=duration_ms)
        except StartupValidationError as exc:
            log.error("startup_failed", error=exc.to_plain_text())
            raise SystemExit(1) from exc

    total_ms = int((time.monotonic() - start_time) * 1000)
    log.info("startup_complete", total_duration_ms=total_ms)

    app.state.registry = registry
    app.state.version = __version__

    # Expose db_session_factory and services for analytics/learn routers
    from vektra_shared.db import get_session_factory as _get_sf

    app.state.db_session_factory = _get_sf()
    app.state.analytics_service = registry.get("analytics", "default")

    # Resolve trace persistence flag (DEBT-011)
    _store = settings.analytics_store_traces
    if _store is None:
        _store = settings.env == "development"
    app.state.store_traces_enabled = _store

    # Expose grounding mode default for API layer resolution (FEAT-020)
    app.state.grounding_mode_default = settings.prompt_grounding_mode

    # Expose show_sources default for learn API resolution (FEAT-014)
    app.state.learn_show_sources_default = settings.learn_show_sources

    if registry.has("learn", "default"):
        app.state.learn_service = registry.get("learn", "default")
        app.state.learn_require_enrollment = settings.learn_require_enrollment

    yield

    # Shutdown
    log.info("shutdown_started")
    from vektra_shared.db import get_engine

    engine = get_engine()
    await engine.dispose()
    log.info("shutdown_complete")


# ---------------------------------------------------------------------------
# Middleware: correlation ID (ARCH-008)
# ---------------------------------------------------------------------------


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Generate or propagate X-Request-ID for every request."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Any,
    ) -> Response:
        raw_id = request.headers.get("x-request-id")

        try:
            request_id = uuid.UUID(raw_id) if raw_id else uuid.uuid4()
        except ValueError:
            request_id = uuid.uuid4()

        request.state.request_id = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=str(request_id))

        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = str(request_id)
        return response


class RateLimitHeaderMiddleware(BaseHTTPMiddleware):
    """Copy X-RateLimit-* headers from request.state to the response.

    The require_scope() dependency stores rate limit headers in
    request.state.rate_limit_headers (dict) after evaluating limits.
    This middleware propagates them to the HTTP response so clients
    see remaining quota on successful (non-429) responses too.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        rl_headers: dict[str, str] | None = getattr(
            request.state, "rate_limit_headers", None
        )
        if rl_headers:
            for header_name, header_value in rl_headers.items():
                response.headers[header_name] = header_value
        return response


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Build the FastAPI application with all routers and middleware."""
    app = FastAPI(
        title="Vektra",
        description="Modular RAG platform",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    # --- Routers ---
    from vektra_admin.api import router as admin_router
    from vektra_admin.ui import (
        get_static_files,
        register_ui_exception_handlers,
        ui_router,
    )
    from vektra_analytics.api import router as analytics_router
    from vektra_core.api import router as core_router
    from vektra_index.api import router as index_router
    from vektra_index.reindex import router as reindex_router
    from vektra_ingest.api import router as ingest_router
    from vektra_learn.api import router as learn_router

    app.include_router(admin_router)
    app.include_router(ui_router)
    app.mount("/admin/static", get_static_files(), name="admin-static")
    register_ui_exception_handlers(app)
    app.include_router(core_router)
    app.include_router(ingest_router)
    app.include_router(index_router)
    app.include_router(reindex_router)
    app.include_router(analytics_router)
    app.include_router(learn_router)

    # Chatbot widget static files (served at /static/learn/vektra-chat.js)
    from fastapi.staticfiles import StaticFiles

    # In editable installs __file__ lives at vektra-app/src/vektra_app/main.py
    # (4 parents to workspace root).  In non-editable Docker installs
    # __file__ is inside site-packages, so we also check /app/vektra-learn/static
    # (the path used in the Dockerfile COPY --from=widget-builder).
    _workspace_root = Path(__file__).resolve().parent.parent.parent.parent
    _docker_widget = Path("/app/vektra-learn/static")
    widget_path = _workspace_root / "vektra-learn" / "static"
    if not widget_path.is_dir() and _docker_widget.is_dir():
        widget_path = _docker_widget
    if widget_path.is_dir():
        app.mount(
            "/static/learn",
            StaticFiles(directory=str(widget_path)),
            name="learn-static",
        )

    # --- Middleware (LIFO: last added = outermost = runs first) ---

    # 1. CORS (innermost)
    cors_origins = _get_cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 2. Prometheus metrics (ARCH-014). prometheus-fastapi-instrumentator
    # replaced starlette-prometheus, which is unmaintained and incompatible
    # with starlette >= 1.0 (route.path AttributeError on included routers).
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator(excluded_handlers=["/metrics"]).instrument(app).expose(
        app, endpoint="/metrics", include_in_schema=False
    )

    # 3. Rate limit response headers (copies X-RateLimit-* from request.state)
    app.add_middleware(RateLimitHeaderMiddleware)

    # 4. Audit middleware (after auth so key_id is in request.state)
    from vektra_admin.middleware import AuditMiddleware

    app.add_middleware(AuditMiddleware)

    # 5. Correlation ID (outermost - sets request_id before anything else)
    app.add_middleware(CorrelationIdMiddleware)

    # --- Global exception handler ---
    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        log.error(
            "unhandled_exception",
            error=str(exc),
            exc_info=True,
            request_id=str(request_id) if request_id else None,
        )
        err = ErrorResponse(
            category=ErrorCategory.CONFIGURATION,
            code=ERR_CONFIG_001,
            message="An internal error occurred.",
            remediation=(
                "Check server logs for details. "
                "If this persists, verify your configuration."
            ),
            request_id=request_id or uuid.uuid4(),
        )
        return JSONResponse(status_code=500, content=err.to_envelope())

    return app


def _get_cors_origins() -> list[str]:
    """Read VEKTRA_CORS_ORIGINS from env (comma-separated)."""
    import os

    raw = os.environ.get("VEKTRA_CORS_ORIGINS", "")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return ["http://localhost:3000"]


app = create_app()

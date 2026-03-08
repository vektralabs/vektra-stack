"""Vektra application entrypoint (ARCH-001, ARCH-015, ARCH-057).

Assembles all component routers, middleware, and providers into a single
FastAPI application. Runs the 8-step startup validation sequence.

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
# ARCH-057: 8-step startup validation
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

    # --- Safeguard ---
    from vektra_shared.safeguards import PassthroughSafeguard

    safeguard = PassthroughSafeguard()
    registry.register("safeguard", "default", safeguard)

    # --- Event emitter ---
    from vektra_shared.events import NoOpEventEmitter

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

    # --- Query pipeline ---
    from vektra_core.conversation import (
        InMemoryConversationStore,
        PersistentConversationStore,
    )
    from vektra_core.pipeline import SimpleQueryPipeline
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
    templates_dir = (
        Path(settings.prompt_templates_dir) if settings.prompt_templates_dir else None
    )
    renderer = TemplateRenderer(templates_dir=templates_dir)
    pipeline_config = QueryPipelineConfig()
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
    registry.register("query_pipeline", "default", pipeline)

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


# ---------------------------------------------------------------------------
# Lifespan: startup + shutdown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Run the 8-step startup validation, then yield for serving."""
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
    from vektra_core.api import router as core_router
    from vektra_index.api import router as index_router
    from vektra_index.reindex import router as reindex_router
    from vektra_ingest.api import router as ingest_router

    app.include_router(admin_router)
    app.include_router(ui_router)
    app.mount("/admin/static", get_static_files(), name="admin-static")
    register_ui_exception_handlers(app)
    app.include_router(core_router)
    app.include_router(ingest_router)
    app.include_router(index_router)
    app.include_router(reindex_router)

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

    # 2. Prometheus metrics
    from starlette_prometheus import PrometheusMiddleware
    from starlette_prometheus.view import metrics as metrics_view

    app.add_middleware(PrometheusMiddleware)
    app.add_route("/metrics", metrics_view)

    # 3. Audit middleware (after auth so key_id is in request.state)
    from vektra_admin.middleware import AuditMiddleware

    app.add_middleware(AuditMiddleware)

    # 4. Correlation ID (outermost - sets request_id before anything else)
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

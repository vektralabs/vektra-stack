"""Health check aggregator for vektra-admin (REQ-004, ARCH-022, ARCH-027).

Reads health-check callables from ProviderRegistry under the "health" category.
Components register their health checks in infra-app-entrypoint (step 5):

    registry.register("health", "llm", litellm_provider.health_check)
    registry.register("health", "embedding", embedding_provider.health_check)
    registry.register("health", "vector_store", vector_store_adapter.health_check)

This aggregator does NOT call component modules directly (ADR-0005 violation).

Two-tier model (REQ-025):
- Shallow  GET /health          — unauthenticated; returns {status, timestamp} only.
- Deep     GET /health?detail=full — authenticated; returns per-component breakdown.
- Single   GET /health/{component} — authenticated; single component status.
- Memory   GET /health/memory       — authenticated; process memory stats.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel

from vektra_shared.types import HealthStatus

log = structlog.get_logger(__name__)

# --- Response models ---------------------------------------------------------


class ComponentHealth(BaseModel):
    """Per-component health result."""

    name: str
    status: str
    latency_ms: int | None = None
    message: str | None = None


class ShallowHealthResponse(BaseModel):
    """Unauthenticated shallow health response."""

    status: str
    timestamp: str


class DeepHealthResponse(BaseModel):
    """Authenticated deep health response with component breakdown."""

    status: str
    timestamp: str
    version: str
    components: list[ComponentHealth]


class MemoryHealthResponse(BaseModel):
    """Process memory stats (ARCH-027)."""

    rss_mb: float
    vms_mb: float
    percent: float


# --- Aggregation logic -------------------------------------------------------


def _aggregate_status(component_statuses: list[str]) -> str:
    """Derive overall status from component statuses.

    - All healthy → healthy
    - Any degraded, rest healthy → degraded
    - Any unhealthy → unhealthy
    """
    if not component_statuses:
        return "healthy"
    if any(s == "unhealthy" for s in component_statuses):
        return "unhealthy"
    if any(s == "degraded" for s in component_statuses):
        return "degraded"
    return "healthy"


async def _call_health_check(
    name: str, checker: Callable[[], Awaitable[HealthStatus]]
) -> ComponentHealth:
    """Call a single health_check() callable, timing it and catching exceptions."""
    start = time.monotonic()
    try:
        result: HealthStatus = await checker()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ComponentHealth(
            name=name,
            status=result.status,
            latency_ms=result.latency_ms
            if result.latency_ms is not None
            else elapsed_ms,
            message=result.message,
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        log.warning("health_check_failed", component=name, error=str(exc))
        return ComponentHealth(
            name=name,
            status="unhealthy",
            latency_ms=elapsed_ms,
            message=str(exc),
        )


async def check_all(
    registry: Any, version: str
) -> tuple[ShallowHealthResponse, DeepHealthResponse]:
    """Run all registered health checks and return both shallow and deep responses.

    The shallow (unauthenticated) check excludes the LLM component because:
    - It makes a paid API call on every probe (Docker health checks run every 10s)
    - External LLM availability shouldn't determine infrastructure health
    - LLM health is still available on-demand via GET /health/llm
    The deep (authenticated) check includes all components.
    """
    import asyncio

    # Shallow: infrastructure only (exclude LLM)
    infra_names = [n for n in registry.list("health") if n != "llm"]
    infra_tasks = [
        _call_health_check(name, registry.get("health", name)) for name in infra_names
    ]
    infra_components: list[ComponentHealth] = await asyncio.gather(*infra_tasks)
    shallow_status = _aggregate_status([c.status for c in infra_components])
    ts = datetime.now(UTC).isoformat()
    shallow = ShallowHealthResponse(status=shallow_status, timestamp=ts)

    # Deep: all components including LLM
    all_names = registry.list("health")
    extra_names = [n for n in all_names if n not in infra_names]
    extra_tasks = [
        _call_health_check(name, registry.get("health", name)) for name in extra_names
    ]
    extra_components: list[ComponentHealth] = await asyncio.gather(*extra_tasks)
    all_components = list(infra_components) + list(extra_components)
    deep_status = _aggregate_status([c.status for c in all_components])

    deep = DeepHealthResponse(
        status=deep_status,
        timestamp=ts,
        version=version,
        components=all_components,
    )
    return shallow, deep


async def check_component(registry: Any, name: str) -> ComponentHealth:
    """Run health check for a single registered component."""
    if not registry.has("health", name):
        return ComponentHealth(
            name=name, status="unknown", message="no health check registered"
        )
    checker = registry.get("health", name)
    return await _call_health_check(name, checker)


def check_memory() -> MemoryHealthResponse:
    """Return current process memory stats via psutil (ARCH-027)."""
    try:
        import os

        import psutil

        proc = psutil.Process(os.getpid())
        info = proc.memory_info()
        pct = proc.memory_percent()
        return MemoryHealthResponse(
            rss_mb=round(info.rss / 1024 / 1024, 2),
            vms_mb=round(info.vms / 1024 / 1024, 2),
            percent=round(pct, 2),
        )
    except ImportError:
        return MemoryHealthResponse(rss_mb=0.0, vms_mb=0.0, percent=0.0)

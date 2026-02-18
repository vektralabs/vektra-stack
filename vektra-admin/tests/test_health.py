"""Unit tests: health check aggregation (REQ-004, ARCH-022, ARCH-027)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from vektra_shared.types import HealthStatus
from vektra_admin.health import (
    ComponentHealth,
    DeepHealthResponse,
    ShallowHealthResponse,
    _aggregate_status,
    check_all,
    check_component,
    check_memory,
)


# --- _aggregate_status ---


def test_aggregate_status_all_healthy():
    assert _aggregate_status(["healthy", "healthy"]) == "healthy"


def test_aggregate_status_any_degraded():
    assert _aggregate_status(["healthy", "degraded"]) == "degraded"


def test_aggregate_status_any_unhealthy():
    assert _aggregate_status(["healthy", "unhealthy", "degraded"]) == "unhealthy"


def test_aggregate_status_empty():
    assert _aggregate_status([]) == "healthy"


# --- check_all ---


async def test_check_all_no_checks_registered():
    registry = MagicMock()
    registry.list.return_value = []

    shallow, deep = await check_all(registry, version="0.1.0")

    assert isinstance(shallow, ShallowHealthResponse)
    assert isinstance(deep, DeepHealthResponse)
    assert shallow.status == "healthy"
    assert deep.components == []
    assert deep.version == "0.1.0"


async def test_check_all_aggregates_correctly():
    async def healthy_checker():
        return HealthStatus(status="healthy", latency_ms=5)

    async def degraded_checker():
        return HealthStatus(status="degraded", message="slow")

    registry = MagicMock()
    registry.list.return_value = ["llm", "embedding"]
    registry.get.side_effect = lambda cat, name: (
        healthy_checker if name == "llm" else degraded_checker
    )

    shallow, deep = await check_all(registry, version="0.1.0")

    assert shallow.status == "degraded"
    assert deep.status == "degraded"
    assert len(deep.components) == 2


async def test_check_all_exception_becomes_unhealthy():
    async def failing_checker():
        raise RuntimeError("connection refused")

    registry = MagicMock()
    registry.list.return_value = ["vector_store"]
    registry.get.return_value = failing_checker

    shallow, deep = await check_all(registry, version="0.1.0")

    assert shallow.status == "unhealthy"
    assert deep.components[0].status == "unhealthy"
    assert "connection refused" in (deep.components[0].message or "")


# --- check_component ---


async def test_check_component_not_registered():
    registry = MagicMock()
    registry.has.return_value = False

    result = await check_component(registry, "unknown")
    assert result.status == "unknown"


async def test_check_component_success():
    async def checker():
        return HealthStatus(status="healthy", latency_ms=10)

    registry = MagicMock()
    registry.has.return_value = True
    registry.get.return_value = checker

    result = await check_component(registry, "embedding")
    assert result.status == "healthy"
    assert result.name == "embedding"


# --- check_memory ---


def test_check_memory_returns_response():
    result = check_memory()
    assert result.rss_mb >= 0
    assert result.vms_mb >= 0
    assert 0 <= result.percent <= 100

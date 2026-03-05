"""Unit tests for vektra_shared.audit (event injection interface)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import vektra_shared.audit as audit_mod


class TestAudit:
    def setup_method(self) -> None:
        self._original = audit_mod._log_fn
        audit_mod._log_fn = None

    def teardown_method(self) -> None:
        audit_mod._log_fn = self._original

    @pytest.mark.asyncio
    async def test_log_event_noop_when_no_fn_registered(self) -> None:
        """log_event is a silent no-op before set_log_fn is called."""
        await audit_mod.log_event(key_id="k1", action="test")  # should not raise

    def test_set_log_fn_registers_callable(self) -> None:
        spy = AsyncMock()
        audit_mod.set_log_fn(spy)
        assert audit_mod._log_fn is spy

    @pytest.mark.asyncio
    async def test_log_event_forwards_kwargs(self) -> None:
        spy = AsyncMock()
        audit_mod.set_log_fn(spy)
        await audit_mod.log_event(key_id="k1", endpoint="/test", action="query")
        spy.assert_called_once_with(key_id="k1", endpoint="/test", action="query")

    @pytest.mark.asyncio
    async def test_log_event_called_multiple_times(self) -> None:
        spy = AsyncMock()
        audit_mod.set_log_fn(spy)
        await audit_mod.log_event(action="a")
        await audit_mod.log_event(action="b")
        assert spy.call_count == 2

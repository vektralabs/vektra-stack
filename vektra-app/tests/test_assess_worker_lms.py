"""The worker's LMS provider lifecycle and binding (PR 2a-2 review, finding 9).

- the setup check binds on the secrets the provider DECLARES (``required_secrets``), not on a
  key name the platform hard-codes;
- a provider loaded at startup is closed when a later startup check refuses, and at shutdown.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_app import assess_worker
from vektra_shared.lms import LmsProvider, guarded
from vektra_shared.startup import StartupValidationError


class _Provider:
    """A provider with lifecycle only; ``closed`` counts ``aclose`` calls."""

    name = "fake"
    setup_items: tuple[str, ...] = ()
    required_secrets: tuple[str, ...] = ("provider_secret",)
    call_deadline_s = 30.0

    def __init__(self) -> None:
        self.closed = 0

    def startup_problems(self, development: bool) -> tuple[Any, ...]:
        return ()

    async def aclose(self) -> None:
        self.closed += 1


for _m in LmsProvider.__protocol_attrs__:  # type: ignore[attr-defined]
    if not hasattr(_Provider, _m):
        setattr(_Provider, _m, lambda *_a, **_k: None)


def _checked(monkeypatch: pytest.MonkeyPatch, keys: dict[str, Any], lms: Any) -> Any:
    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    url = "postgresql+asyncpg://vektra_assess_worker:x@127.0.0.1:1/db"
    return assess_worker.Checked(
        create_async_engine(url), VektraAssessSettings(), url, keys, lms=lms
    )


def test_the_setup_check_binds_on_the_secrets_the_provider_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_assess.jobs import JobKind
    from vektra_assess.keys import Credentials

    lms = guarded(_Provider())  # type: ignore[arg-type]
    without = _checked(monkeypatch, {}, lms)
    assert assess_worker._setup_check_parts(without).handlers == {}
    # a key the platform used to hard-code binds nothing when the provider does not declare it
    other = _checked(
        monkeypatch, {"assess_ws_tokens": Credentials("assess_ws_tokens", {})}, lms
    )
    assert assess_worker._setup_check_parts(other).handlers == {}
    declared = _checked(
        monkeypatch, {"provider_secret": Credentials("provider_secret", {})}, lms
    )
    assert JobKind.SETUP_CHECK in assess_worker._setup_check_parts(declared).handlers


def test_every_lms_reading_handler_is_unbound_without_a_declared_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second review, finding 4: one gate at composition (``_bound_lms``). With a session key
    set complete but the provider's declared secret missing, the pre-flight (and every other
    handler that reads the LMS) is not bound; with it, the pre-flight is."""
    import os

    from vektra_assess.jobs import JobKind
    from vektra_assess.keys import AssessKeyring, Credentials

    monkeypatch.setenv("VEKTRA_ASSESS_LLM_MODEL", "openai/test-model")
    ring = AssessKeyring("k", "k1", {"k1": os.urandom(32)})
    session_keys: dict[str, Any] = {n: ring for n in assess_worker.SESSION_KEYS}
    lms = guarded(_Provider())  # type: ignore[arg-type]
    missing = _checked(monkeypatch, dict(session_keys), lms)
    assert assess_worker._bound_lms(missing) is None
    assert assess_worker._session_handlers(missing, audit=None) == {}
    keyed = _checked(
        monkeypatch,
        {**session_keys, "provider_secret": Credentials("provider_secret", {})},
        lms,
    )
    assert assess_worker._bound_lms(keyed) is lms
    import vektra_assess.worker.preflight as preflight_mod

    seen: list[Any] = []
    real = preflight_mod.make_preflight_handler

    def capture(deps: Any) -> Any:
        seen.append(deps)
        return real(deps)

    monkeypatch.setattr(preflight_mod, "make_preflight_handler", capture)
    handlers = assess_worker._session_handlers(
        keyed, audit=None, preflight_budget_s=7.5
    )
    # the budget the composed runtime enforces reaches the handler (fifth review of #221, 7)
    assert [d.job_budget_s for d in seen] == [7.5]
    for kind in (
        JobKind.PREFLIGHT,
        JobKind.ENROLMENT_RECHECK,
        JobKind.RENDER_CHECK,
        JobKind.TEACHER_GATE,
        JobKind.TRANSCRIPT_GENERATE,
    ):
        assert kind in handlers, kind


async def test_a_later_startup_refusal_closes_the_loaded_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_assess import keys

    provider = _Provider()
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setattr(keys, "enforce_key_matrix", lambda *a, **k: {})

    def find(settings: Any, entry_points: Any = None) -> Any:
        return object()  # an entry-point object declaring no secret

    async def build(settings: Any, factory: Any) -> Any:
        return provider

    async def refuse(*_a: Any) -> Any:
        raise StartupValidationError(step="x", detail="refused", remediation="fix")

    monkeypatch.setattr(assess_worker, "_find_lms", find)
    monkeypatch.setattr(assess_worker, "_build_lms", build)
    monkeypatch.setattr(assess_worker, "_checks_with_lms", refuse)
    with pytest.raises(StartupValidationError):
        await assess_worker._checks()
    assert provider.closed == 1


async def test_the_worker_closes_its_provider_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    engine = SimpleNamespace(dispose=lambda: asyncio.sleep(0))
    checked = SimpleNamespace(engine=engine, liveness=None, lms=provider)

    async def checks(*_a: Any) -> Any:
        return checked

    async def serve(*_a: Any) -> None:
        return None

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setattr(assess_worker, "_checks", checks)
    monkeypatch.setattr(assess_worker, "_serve", serve)
    assert await assess_worker._amain() == 0
    assert provider.closed == 1

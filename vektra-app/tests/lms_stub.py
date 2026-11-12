"""A minimal LMS provider for the worker's startup and composition tests (ADR-0033 D6).

The platform installs no provider: the Moodle one is a separate distribution (``adapters/``,
``--group lms-moodle``). The worker tests still need one to reach the steps after the provider
load, so this stub is offered through ``install`` (the loader's own ``entry_points`` seam) under
the name ``stub``. It is NOT marked as a test double, so it loads with ``VEKTRA_ENV=production``
like a real provider; it reads nothing (every read member is a stub that returns ``None``), and
it declares one secret, so the key matrix's provider half (plan task 2a-4b.13) is exercised.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vektra_shared.lms import LmsProvider

NAME = "stub"
STUB_SECRET = "assess_stub_lms_tokens"


class StubLmsProvider:
    name = NAME
    setup_items: tuple[str, ...] = ()
    required_secrets: tuple[str, ...] = (STUB_SECRET,)

    def startup_problems(self, development: bool) -> tuple[Any, ...]:
        return ()

    async def aclose(self) -> None:
        return None


for _m in LmsProvider.__protocol_attrs__:  # type: ignore[attr-defined]
    if not hasattr(StubLmsProvider, _m):
        setattr(StubLmsProvider, _m, lambda *_a, **_k: None)


@dataclass
class _EP:
    name: str

    def load(self) -> Any:
        return StubLmsProvider


def entry_points() -> list[_EP]:
    return [_EP(NAME)]


def install(setattr_: Any = None) -> None:
    """Make ``vektra_app.lms_providers.load_lms_provider`` see the stub as the installed group.
    ``setattr_`` is ``monkeypatch.setattr`` in a test (undone after it); a subprocess passes
    nothing and patches for its lifetime."""
    from vektra_app import lms_providers

    real = lms_providers.load_lms_provider

    async def load(name: str | None, **kw: Any) -> Any:
        kw.setdefault("entry_points", entry_points)
        return await real(name, **kw)

    (setattr_ or setattr)(lms_providers, "load_lms_provider", load)


def write_secret(secrets_dir: Path) -> None:
    """The stub's declared secret, as the key matrix requires it (an empty credentials object)."""
    path = secrets_dir / STUB_SECRET
    path.write_text("{}")
    os.chmod(path, 0o400)

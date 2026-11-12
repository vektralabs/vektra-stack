"""Assess parser sidecar entry (plan assess-parser, COMP-008, ARCH-066).

The parser parses untrusted submission files. It runs with no network, no secrets and no
DSN, and talks only over a unix socket shared with the worker. Before binding it proves
that it holds nothing (``enforce_key_matrix("parser")``: secrets dir absent or empty, no
key or DSN env var) and that nothing it must not hold has been imported
(``assert_import_ceiling``). The serving code is ``vektra_assess.parser_sidecar.server``;
this module is only the composition root.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vektra_assess.config import VektraAssessSettings
from vektra_shared.startup import StartupValidationError

if TYPE_CHECKING:
    from vektra_assess.parser_sidecar.server import ServerConfig

STEP = "assess_parser_check"

# What the sidecar must never have loaded: database, HTTP client, LLM, vector and ML
# stacks, the API, other components. Checked against sys.modules before serving.
FORBIDDEN_MODULES: tuple[str, ...] = (
    "sqlalchemy",
    "asyncpg",
    "httpx",
    "litellm",
    "vektra_shared.db",
    "vektra_ingest",
    "vektra_index",
    "vektra_core",
    "vektra_admin",
    "vektra_learn",
    "vektra_analytics",
    "vektra_app.main",
    "torch",
    "sentence_transformers",
    "transformers",
    "fastapi",
    "uvicorn",
    "aiosmtplib",
)
# (cryptography is deliberately absent: pdfminer.six imports it for encrypted PDFs.)
# The only vektra_assess modules the parser process may hold: its own packages plus the
# two the entry itself uses for the "holds nothing" proof.
ALLOWED_ASSESS = ("vektra_assess.parser_protocol", "vektra_assess.parser_sidecar")
LMS_PROVIDER_PACKAGE_PREFIX = "vektra_lms_"
ALLOWED_ASSESS_EXACT = ("vektra_assess", "vektra_assess.config", "vektra_assess.keys")


def assert_import_ceiling(loaded: Iterable[str] | None = None) -> None:
    modules = set(sys.modules if loaded is None else loaded)
    present = sorted(
        m
        for m in modules
        if m in FORBIDDEN_MODULES
        or any(m.startswith(f + ".") for f in FORBIDDEN_MODULES)
    )
    stray = sorted(
        m
        for m in modules
        if (
            m.startswith("vektra_assess.")
            and m not in ALLOWED_ASSESS_EXACT
            and not any(m == a or m.startswith(a + ".") for a in ALLOWED_ASSESS)
        )
        # an LMS provider (its client, its credentials reader) is never the parser's, whatever
        # the provider is called (ADR-0033 D6: providers are ``vektra_lms_*`` distributions)
        or m.split(".", 1)[0].startswith(LMS_PROVIDER_PACKAGE_PREFIX)
    )
    if present or stray:
        raise StartupValidationError(
            step=STEP,
            detail=f"forbidden modules loaded in the assess parser: {present + stray}",
            remediation=(
                "The parser must hold no database, network client, ML stack or other "
                "component (ARCH-066, ARCH-073). Find the chain with `python -X importtime`."
            ),
        )


#: What a parsing CHILD must not hold, beyond the process ceiling. The forkserver preloads
#: ``vektra_assess.parser_sidecar.child`` (package ``__init__`` included) and every child inherits
#: it: the child holds the parser and the stdlib, never the shared kernel or a logging stack
#: (review of #203: a registration in ``vektra_assess/__init__`` pulled both in).
CHILD_FORBIDDEN_MODULES: tuple[str, ...] = (
    *FORBIDDEN_MODULES,
    "vektra_shared",
    "vektra_app",
    "structlog",
    "pydantic_settings",
)


def assert_child_import_ceiling(loaded: Iterable[str]) -> None:
    """The ceiling of a parsing child, checked on what the forkserver preload leaves loaded."""
    modules = set(loaded)
    present = sorted(
        m
        for m in modules
        if m in CHILD_FORBIDDEN_MODULES
        or any(m.startswith(f + ".") for f in CHILD_FORBIDDEN_MODULES)
    )
    if present:
        raise StartupValidationError(
            step=STEP,
            detail=f"forbidden modules loaded in a parsing child: {present}",
            remediation=(
                "A parsing child holds the parser and the stdlib only (ARCH-066). Keep imports "
                "out of package __init__ modules on the child's import path."
            ),
        )


def check_environment() -> VektraAssessSettings:
    """Refuse to run unless enabled and holding nothing. Returns the loaded settings."""
    from vektra_assess.keys import enforce_key_matrix

    settings = VektraAssessSettings()
    if not settings.enabled:
        raise RuntimeError(
            "VEKTRA_ASSESS_ENABLED is not true: the assess parser does not run on a "
            "deployment where assess is disabled."
        )
    enforce_key_matrix("parser", secrets_dir=settings.secrets_dir, step=STEP)
    return settings


def build_server_config(settings: VektraAssessSettings) -> ServerConfig:
    """The single definition of the parser limits is ``VektraAssessSettings``."""
    from vektra_assess.parser_sidecar.server import ServerConfig

    return ServerConfig(
        socket_path=settings.parser_socket,
        pool=settings.parser_pool,
        timeout_s=settings.parser_timeout_s,
        max_chars=settings.parser_max_chars,
        max_file_bytes=settings.parser_max_file_bytes,
        max_pages=settings.parser_max_pages,
        rlimit_as=settings.parser_rlimit_as_mb * 1024 * 1024,
    )


def main() -> None:
    try:
        settings = check_environment()
        from vektra_assess.parser_sidecar import server

        cfg = build_server_config(settings)
        assert_import_ceiling()
        code = server.main(cfg)
    except ValidationError as exc:
        print(
            "[ASSESS PARSER] invalid VEKTRA_ASSESS_* setting: "
            + "; ".join(
                f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}"
                for e in exc.errors()
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except RuntimeError as exc:
        print(f"[ASSESS PARSER] {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except StartupValidationError as exc:
        print(exc.to_plain_text(), file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(code)


if __name__ == "__main__":
    main()

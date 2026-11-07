"""Loads the generated parser corpus (fixtures/parser/build_corpus.py) once per session."""

from __future__ import annotations

import functools
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_PATH = Path(__file__).parent / "fixtures" / "parser" / "build_corpus.py"


@functools.cache
def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("assess_parser_build_corpus", _PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@functools.cache
def corpus() -> dict[str, Any]:  # type: ignore[valid-type]
    return {it.name: it for it in module().build_corpus()}


# Items that need real children (rlimits, wall-clock kill, size gate): not run in-process.
HEAVY = frozenset({"pdf_huge_stream", "pdf_slow_content", "file_11mb"})

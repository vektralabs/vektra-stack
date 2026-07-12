"""Jinja2 template loader and renderer for RAG prompt composition (ARCH-054, ADR-0020).

Two composable templates:
  system.j2  - system instructions with grounding mode and namespace context
  context.j2 - retrieved chunk injection

Conversation history is passed as native chat messages (user/assistant pairs),
not rendered via a template (DEBT-016).

Template directory priority (ARCH-054):
  1. VEKTRA_PROMPT_TEMPLATES_DIR if set (operator override)
  2. Built-in defaults in vektra_core/templates/

prompt_version = SHA-256(concatenated template sources)[:8] (ARCH-048).
Computed on load; validated in ARCH-057 step 8.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import jinja2
import structlog

log = structlog.get_logger(__name__)

_BUILTIN_DIR = Path(__file__).parent / "templates"
_TEMPLATE_NAMES = ("system", "context")
_OPTIONAL_TEMPLATES = ("rewrite",)


class TemplateRenderer:
    """Loads and renders RAG prompt templates (system.j2, context.j2).

    Thread-safe: templates are loaded once at construction, rendering is stateless.
    Raises FileNotFoundError if a required template file is missing.
    """

    def __init__(self, templates_dir: Path | None = None) -> None:
        search_dir = templates_dir if templates_dir is not None else _BUILTIN_DIR

        self._env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(search_dir)),
            autoescape=False,
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

        # Compute per-template SHA-256[:8] and the combined prompt_version (ARCH-048)
        self._per_version: dict[str, str] = {}
        combined_content = ""
        for name in _TEMPLATE_NAMES:
            path = search_dir / f"{name}.j2"
            if not path.exists():
                raise FileNotFoundError(
                    f"Required template not found: {path}. "
                    "Set VEKTRA_PROMPT_TEMPLATES_DIR or provide built-in templates."
                )
            content = path.read_text(encoding="utf-8")
            self._per_version[name] = hashlib.sha256(content.encode()).hexdigest()[:8]
            combined_content += content

        # Include optional templates (rewrite.j2) in hash if they exist (ARCH-048)
        for name in _OPTIONAL_TEMPLATES:
            path = search_dir / f"{name}.j2"
            if path.exists():
                content = path.read_text(encoding="utf-8")
                self._per_version[name] = hashlib.sha256(content.encode()).hexdigest()[
                    :8
                ]
                combined_content += content

        self._prompt_version = hashlib.sha256(combined_content.encode()).hexdigest()[:8]
        log.info(
            "templates_loaded",
            dir=str(search_dir),
            prompt_version=self._prompt_version,
        )

    @property
    def prompt_version(self) -> str:
        """Combined SHA-256[:8] of required and optional template sources (ARCH-048)."""
        return self._prompt_version

    def render_template(self, name: str, **kwargs: Any) -> str:
        """Render an arbitrary template by filename (e.g. 'rewrite.j2')."""
        tmpl = self._env.get_template(name)
        return tmpl.render(**kwargs)

    def render_system(
        self,
        namespace: str = "default",
        grounding_mode: str = "strict",
        has_context: bool = True,
        citations_enabled: bool = False,
    ) -> str:
        """Render system.j2 with namespace, grounding mode, and context/citation flags."""
        tmpl = self._env.get_template("system.j2")
        return tmpl.render(
            namespace=namespace,
            grounding_mode=grounding_mode,
            has_context=has_context,
            citations_enabled=citations_enabled,
        )

    def render_context(self, chunks: list[dict[str, Any]]) -> str:
        """Render context.j2 with a list of chunk dicts containing 'text' and
        optionally 'score' and 'title' (source title attribute, FEAT-021)."""
        tmpl = self._env.get_template("context.j2")
        return tmpl.render(chunks=chunks)

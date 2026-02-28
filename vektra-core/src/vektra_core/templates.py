"""Jinja2 template loader and renderer for RAG prompt composition (ARCH-054, ADR-0020).

Three composable templates:
  system.j2      - system instructions with namespace context
  context.j2     - retrieved chunk injection
  conversation.j2 - multi-turn conversation history

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
_TEMPLATE_NAMES = ("system", "context", "conversation")


class TemplateRenderer:
    """Loads and renders the three RAG prompt templates.

    Thread-safe: templates are loaded once at construction, rendering is stateless.
    Raises FileNotFoundError if a required template file is missing.
    """

    def __init__(self, templates_dir: Path | None = None) -> None:
        search_dir = templates_dir if templates_dir is not None else _BUILTIN_DIR

        self._env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(search_dir)),
            autoescape=False,
            undefined=jinja2.StrictUndefined,
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

        self._prompt_version = hashlib.sha256(combined_content.encode()).hexdigest()[:8]
        log.info(
            "templates_loaded",
            dir=str(search_dir),
            prompt_version=self._prompt_version,
        )

    @property
    def prompt_version(self) -> str:
        """Combined SHA-256[:8] of all three template sources (ARCH-048)."""
        return self._prompt_version

    def render_system(self, namespace: str = "default") -> str:
        """Render system.j2 with namespace variable."""
        tmpl = self._env.get_template("system.j2")
        return tmpl.render(namespace=namespace)

    def render_context(self, chunks: list[dict[str, Any]]) -> str:
        """Render context.j2 with a list of chunk dicts containing 'text' (and optionally 'score')."""
        tmpl = self._env.get_template("context.j2")
        return tmpl.render(chunks=chunks)

    def render_conversation(self, history: list[dict[str, Any]]) -> str:
        """Render conversation.j2 with a list of turn dicts containing 'question' and 'answer'."""
        tmpl = self._env.get_template("conversation.j2")
        return tmpl.render(history=history)

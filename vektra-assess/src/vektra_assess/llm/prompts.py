"""Prompt rendering for the discussion runner (ADR-0020, mirrored locally: ARCH-073(b)).

vektra_core's ``TemplateRenderer`` is not imported (the assess vertical imports no other component),
so this module mirrors it: ``StrictUndefined``, autoescape off, and here a SANDBOXED environment,
because the templates interpolate snapshot text and the student's messages.

The model data come from ``bundle_to_model_payload`` only (the single serializer of plan 6): the
templates wrap it, they add no other data source.

``prompt_version`` is SHA-256[:8] over every template, every shipped string file and the
post-check lexicon version, so changing a template, a core sentence or a lexicon entry changes the
version and requires a new NFR-023 activation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import jinja2
from jinja2.sandbox import SandboxedEnvironment

from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.payload import bundle_to_model_payload
from vektra_assess.context.references import canonical_labels
from vektra_assess.llm.types import PriorTurn
from vektra_shared.types import Message

TEMPLATES_DIR = Path(__file__).parent / "templates"
STRINGS_DIR = Path(__file__).parent / "strings"
TEMPLATE_NAMES = ("system", "bundle", "history", "turn", "part_b")
LANGUAGE_NAMES = {"it": "italiano", "en": "inglese"}
WORD_TARGET = 150  # REQ-100: about 150 words unless asked
PART_B_MAX_WORDS = 120  # REQ-094
PART_B_MAX_LABELS = 3


def compute_prompt_version(
    templates_dir: Path, strings_dir: Path, lexicon_version: str
) -> str:
    """SHA-256[:8] over the template sources, the string files and the lexicon version.

    Each file enters as its name and its bytes, in name order, so renaming, editing, adding or
    removing a file all change the version; a missing strings directory counts as empty.
    """
    h = hashlib.sha256()
    for name in TEMPLATE_NAMES:
        path = templates_dir / f"{name}.j2"
        h.update(f"template:{name}\0".encode())
        h.update(path.read_bytes())  # a missing template is an error, never skipped
    strings = sorted(strings_dir.glob("*.yaml")) if strings_dir.is_dir() else []
    for path in strings:
        h.update(f"\0strings:{path.name}\0".encode())
        h.update(path.read_bytes())
    h.update(f"\0lexicon:{lexicon_version}".encode())
    return h.hexdigest()[:8]


class PromptRenderer:
    """Renders the turn and Part B prompts. Templates are loaded once; rendering is stateless."""

    def __init__(
        self,
        lexicon_version: str,
        templates_dir: Path = TEMPLATES_DIR,
        strings_dir: Path = STRINGS_DIR,
    ) -> None:
        self._env = SandboxedEnvironment(
            loader=jinja2.FileSystemLoader(str(templates_dir)),
            autoescape=False,
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
        )
        self._templates = {n: self._env.get_template(f"{n}.j2") for n in TEMPLATE_NAMES}
        self._version = compute_prompt_version(
            templates_dir, strings_dir, lexicon_version
        )

    @property
    def prompt_version(self) -> str:
        return self._version

    def _render(self, name: str, **kwargs: object) -> str:
        return self._templates[name].render(**kwargs).strip()

    def _system(self, bundle: ContextBundle, language: str) -> Message:
        return Message(
            role="system",
            content=self._render(
                "system",
                reply_language_name=LANGUAGE_NAMES[language],
                word_target=WORD_TARGET,
                labels=canonical_labels(bundle),
            ),
        )

    def _bundle(self, bundle: ContextBundle) -> Message:
        (payload,) = bundle_to_model_payload(bundle)  # one user-role context message
        return Message(
            role="user", content=self._render("bundle", payload=payload.content)
        )

    def turn_messages(
        self,
        bundle: ContextBundle,
        history: Sequence[PriorTurn],
        text: str,
        language: str,
    ) -> list[Message]:
        """System, context, the kept history (oldest first) and the student's message."""
        out = [self._system(bundle, language), self._bundle(bundle)]
        for prior in history:
            out.append(
                Message(
                    role="user" if prior.role == "student" else "assistant",
                    content=self._render("history", role=prior.role, text=prior.text),
                )
            )
        out.append(Message(role="user", content=self._render("turn", text=text)))
        return out

    def part_b_messages(self, bundle: ContextBundle, language: str) -> list[Message]:
        return [
            self._system(bundle, language),
            self._bundle(bundle),
            Message(
                role="user",
                content=self._render(
                    "part_b",
                    reply_language_name=LANGUAGE_NAMES[language],
                    max_words=PART_B_MAX_WORDS,
                    max_labels=PART_B_MAX_LABELS,
                ),
            ),
        ]

"""``DiscussionTurnRunner``: the only code path that sends discussion content to a model (COMP-006).

Not a QueryPipeline and not in the ProviderRegistry (ARCH-074): the composition roots in vektra_app
inject the LLM provider, the safeguard, the trace writer and the audit sink, so this module imports
no other component. Its only input type is ``ContextBundle`` (ARCH-069).

RAG is OFF in v1 and there is no retrieval branch here at all: a bundle frozen with
``course_material`` set is refused with ``RagNotAvailableError`` (NFR-015), never answered without
the material the session was promised.
"""

from __future__ import annotations

from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.llm.prompts import PromptRenderer
from vektra_assess.llm.types import AuditSink, RagNotAvailableError, TraceWriter
from vektra_shared.protocols import LLMProvider, SafeguardHook

# until the post-check lexicon exists (task B), the renderer is versioned with this placeholder
_LEXICON_VERSION = "none"


class DiscussionTurnRunner:
    def __init__(
        self,
        llm: LLMProvider,
        safeguard: SafeguardHook,
        trace_writer: TraceWriter,
        settings: VektraAssessSettings,
        audit: AuditSink,
        renderer: PromptRenderer | None = None,
    ) -> None:
        if not settings.llm_model:
            raise ValueError("VEKTRA_ASSESS_LLM_MODEL is not set")
        self._llm = llm
        self._safeguard = safeguard
        self._trace_writer = trace_writer
        self._settings = settings
        self._audit = audit
        self._renderer = renderer or PromptRenderer(_LEXICON_VERSION)

    @property
    def model(self) -> str:
        return str(self._settings.llm_model)

    @property
    def prompt_version(self) -> str:
        return self._renderer.prompt_version

    @staticmethod
    def _admit(bundle: ContextBundle) -> None:
        """Fail closed on anything v1 cannot honour before a byte is rendered."""
        if bundle.course_material:
            raise RagNotAvailableError("course material (RAG) is not available in v1")

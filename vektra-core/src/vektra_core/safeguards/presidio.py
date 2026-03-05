"""PresidioPIISafeguard: PII detection and anonymization via Microsoft Presidio.

Implements SafeguardHook Protocol (ARCH-049). Three trust boundary points:
- pre_query: pass-through (Presidio is for output, not input filtering)
- post_retrieval: scan chunk text for PII, filter chunks exceeding threshold
- pre_response: anonymize PII entities in LLM answer via modified_content
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from vektra_shared.types import SafeguardContext, SafeguardResult, SearchResult

if TYPE_CHECKING:
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine

log = structlog.get_logger(__name__)

_DEFAULT_PII_CHUNK_THRESHOLD = 3
_DEFAULT_LANGUAGE = "en"
_DEFAULT_SCORE_THRESHOLD = 0.5


class PresidioPIISafeguard:
    """SafeguardHook that detects and anonymizes PII using Microsoft Presidio.

    Engines are lazy-loaded on first call to avoid import overhead when not used.
    If Presidio fails to load, methods fall through as pass-through with a warning.
    """

    def __init__(
        self,
        *,
        pii_chunk_threshold: int = _DEFAULT_PII_CHUNK_THRESHOLD,
        language: str = _DEFAULT_LANGUAGE,
        score_threshold: float = _DEFAULT_SCORE_THRESHOLD,
        spacy_model: str | None = None,
    ) -> None:
        self._pii_chunk_threshold = pii_chunk_threshold
        self._language = language
        self._score_threshold = score_threshold
        self._spacy_model = spacy_model
        self._analyzer: AnalyzerEngine | None = None
        self._anonymizer: AnonymizerEngine | None = None
        self._initialized = False

    def _ensure_engines(self) -> bool:
        """Lazy-load Presidio engines. Returns True if available."""
        if self._initialized:
            return self._analyzer is not None

        self._initialized = True
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider
            from presidio_anonymizer import AnonymizerEngine

            # Try explicit model, then en_core_web_lg, then en_core_web_sm
            model_name = self._spacy_model
            if model_name is None:
                import spacy.util

                for candidate in ("en_core_web_lg", "en_core_web_sm"):
                    if spacy.util.is_package(candidate):
                        model_name = candidate
                        break

            if model_name is None:
                raise RuntimeError(
                    "No spaCy model found (need en_core_web_lg or en_core_web_sm)"
                )

            nlp_config = {
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": self._language, "model_name": model_name}],
            }
            nlp_engine = NlpEngineProvider(nlp_configuration=nlp_config).create_engine()
            self._analyzer = AnalyzerEngine(nlp_engine=nlp_engine)
            self._anonymizer = AnonymizerEngine()  # type: ignore[no-untyped-call]
            log.info("presidio_engines_loaded", spacy_model=model_name)
            return True
        except (Exception, SystemExit) as exc:
            # SystemExit: spaCy calls sys.exit(1) when model download fails
            log.warning("presidio_engines_failed", error=str(exc))
            return False

    def _count_pii_entities(self, text: str) -> tuple[int, list[dict[str, Any]]]:
        """Analyze text for PII entities. Returns (count, entity_details)."""
        if not self._analyzer:
            return 0, []

        results = self._analyzer.analyze(
            text=text,
            language=self._language,
            score_threshold=self._score_threshold,
        )
        entities = [
            {"type": r.entity_type, "score": r.score, "start": r.start, "end": r.end}
            for r in results
        ]
        return len(results), entities

    async def pre_query(
        self,
        query_ref: str,
        context: SafeguardContext,
    ) -> SafeguardResult:
        """Pass-through: Presidio is for output, not input filtering."""
        return SafeguardResult(allowed=True)

    async def post_retrieval(
        self,
        query_ref: str,
        results: list[SearchResult],
        context: SafeguardContext,
    ) -> SafeguardResult:
        """Filter chunks with PII entity count above threshold."""
        if not self._ensure_engines():
            return SafeguardResult(allowed=True)

        filtered_ids: list[str] = []
        total_entities = 0

        for result in results:
            count, _ = self._count_pii_entities(result.text_snippet)
            total_entities += count
            if count >= self._pii_chunk_threshold:
                filtered_ids.append(result.chunk_id)

        if filtered_ids:
            log.info(
                "presidio_post_retrieval_filtered",
                filtered_count=len(filtered_ids),
                total_entities=total_entities,
            )

        return SafeguardResult(
            allowed=True,
            filtered_ids=filtered_ids if filtered_ids else None,
            annotations={"pii_entities_found": total_entities},
        )

    async def pre_response(
        self,
        response_ref: str,
        context: SafeguardContext,
    ) -> SafeguardResult:
        """Anonymize PII entities in LLM answer text."""
        if not self._ensure_engines() or not response_ref:
            return SafeguardResult(allowed=True)

        try:
            analyzer_results = self._analyzer.analyze(  # type: ignore[union-attr]
                text=response_ref,
                language=self._language,
                score_threshold=self._score_threshold,
            )

            if not analyzer_results:
                return SafeguardResult(allowed=True)

            anonymized = self._anonymizer.anonymize(  # type: ignore[union-attr]
                text=response_ref,
                analyzer_results=analyzer_results,  # type: ignore[arg-type]
            )

            entity_types = list({r.entity_type for r in analyzer_results})
            log.info(
                "presidio_pre_response_anonymized",
                entity_count=len(analyzer_results),
                entity_types=entity_types,
            )

            return SafeguardResult(
                allowed=True,
                modified_content=anonymized.text,
                annotations={
                    "pii_entity_types": entity_types,
                    "pii_entity_count": len(analyzer_results),
                },
            )
        except Exception as exc:
            log.error("presidio_pre_response_failed", error=str(exc))
            return SafeguardResult(allowed=True)

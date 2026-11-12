"""``ContextBundle``: the ONLY input the discussion LLM adapter accepts (ARCH-069, ARCH-074).

Its constructor needs a sentinel held privately by ``vektra_assess.context._assembly``, so the
only way to obtain a bundle is ``assemble_context(snapshot, frozen_policy)``. Every Pydantic escape
hatch that could build or widen one without that path is closed: ``model_construct``,
``model_copy``, ``model_validate`` / ``model_validate_json`` / ``model_validate_strings``, copy,
deepcopy and pickling all raise. import-linter cannot see call sites, so an AST guard test scans
the repository for constructions outside this package (allowlist: empty).

Fields are a strict subset of the snapshot: no LMS id, no free-form dict, no retrieval field (RAG
is OFF in v1, NFR-015: a future retrieval field comes with its own canary cells).
"""

from __future__ import annotations

from typing import Any, NoReturn

from pydantic import BaseModel, ConfigDict

from vektra_assess.context.snapshot import (
    ActivityKind,
    AssignCriterion,
    FileEntry,
    GradeMethod,
    OverallItem,
    QuizItem,
    SubmissionItem,
)


class BundleConstructionError(TypeError):
    """A ContextBundle was built outside ``assemble_context``."""


_KEY: list[object] = []  # filled once by _assembly; holds the sentinel object


def _refuse(*_a: Any, **_k: Any) -> NoReturn:
    raise BundleConstructionError("a ContextBundle is built only by assemble_context()")


class ContextBundle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    activity_kind: ActivityKind
    attempt_number: int
    attempt_count: int
    grade_method: GradeMethod | None
    quiz_items: tuple[QuizItem, ...]
    criteria: tuple[AssignCriterion, ...]
    overall: OverallItem
    submission: SubmissionItem | None
    files: tuple[FileEntry, ...]
    solution_text: str | None
    pasted_rubric: str | None
    solution_shown: bool
    html_structured_only: bool
    # RAG for the discussion, frozen at Start. Locked OFF in v1: the runner refuses a bundle with it
    # set (RagNotAvailableError) instead of answering without the material it was promised
    course_material: bool = False
    content_hash: str  # of the snapshot it was assembled from

    def __init__(self, *, _sentinel: object = None, **data: Any) -> None:
        if not _KEY or _sentinel is not _KEY[0]:
            _refuse()
        super().__init__(**data)

    # --- closed escape hatches -----------------------------------------------------------------
    @classmethod
    def model_construct(cls, *_a: Any, **_k: Any) -> NoReturn:  # type: ignore[override]
        _refuse()

    @classmethod
    def model_validate(cls, *_a: Any, **_k: Any) -> NoReturn:
        _refuse()

    @classmethod
    def model_validate_json(cls, *_a: Any, **_k: Any) -> NoReturn:
        _refuse()

    @classmethod
    def model_validate_strings(cls, *_a: Any, **_k: Any) -> NoReturn:
        _refuse()

    def model_copy(self, *_a: Any, **_k: Any) -> NoReturn:
        _refuse()

    def __copy__(self) -> NoReturn:
        _refuse()

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> NoReturn:
        _refuse()

    def __reduce__(self) -> NoReturn:
        _refuse()

    def __reduce_ex__(self, protocol: Any) -> NoReturn:
        _refuse()

    def labels(self) -> tuple[str, ...]:
        return tuple(i.label for i in self.quiz_items) + tuple(
            c.label for c in self.criteria
        )

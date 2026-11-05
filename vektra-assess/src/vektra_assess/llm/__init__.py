"""Discussion turn runner, prompts and post-check (COMP-006, ARCH-074).

Owning plan: .s2s/plans/20261101-154950-assess-llm-runner. The only code path that sends
discussion content to a model. It accepts a ``ContextBundle`` only and has no retrieval branch.
"""

from vektra_assess.llm.types import (
    PartBRejectedError,
    ProviderCallRecord,
    ProviderOutageError,
    RagNotAvailableError,
    ReleasedSegment,
    StudentTurn,
    TraceWriter,
    TurnResult,
    TurnUsage,
)

# DiscussionTurnRunner joins this list when it exists (task A, runner core)
__all__ = [
    "PartBRejectedError",
    "ProviderCallRecord",
    "ProviderOutageError",
    "RagNotAvailableError",
    "ReleasedSegment",
    "StudentTurn",
    "TraceWriter",
    "TurnResult",
    "TurnUsage",
]

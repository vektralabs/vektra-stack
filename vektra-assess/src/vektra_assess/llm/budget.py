"""Token budget of one prompt (plan assess-llm-runner, task A).

The completion's ``max_tokens`` is reserved out of the context window first. What remains must
hold the system message, the context bundle and the student's message: if it does not, the turn
fails with ``ContextOverflowError``. The bundle is NEVER trimmed, because a model answering from a
truncated snapshot would answer about data it cannot see. History is what gives way: earlier turns
are kept newest first while they fit, and dropped oldest first.

Counts come from ``LLMProvider.count_tokens``, an estimate (len // 4 when litellm has no tokenizer
for the model), so each message is charged a small fixed overhead on top.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from vektra_shared.types import Message

MESSAGE_OVERHEAD = 4  # role and separators, per message, as chat formats charge


class ContextOverflowError(RuntimeError):
    """The fixed part of the prompt (system, bundle, current message) exceeds the budget."""

    def __init__(self, needed: int, available: int) -> None:
        super().__init__(f"needed={needed} available={available}")
        self.needed = needed
        self.available = available


@dataclass(frozen=True)
class BudgetedPrompt:
    messages: list[Message]
    prompt_tokens: int
    history_kept: int
    history_dropped: int


def fit(
    head: Sequence[Message],
    history: Sequence[Message],
    tail: Sequence[Message],
    *,
    count: Callable[[str], int],
    context_window: int,
    max_tokens: int,
) -> BudgetedPrompt:
    """``head`` + the newest history that fits + ``tail``; ``head`` and ``tail`` are never cut."""

    def cost(m: Message) -> int:
        return count(m.content) + MESSAGE_OVERHEAD

    available = context_window - max_tokens
    fixed = sum(cost(m) for m in (*head, *tail))
    if fixed > available:
        raise ContextOverflowError(fixed, available)
    used, kept = fixed, 0
    for m in reversed(history):
        c = cost(m)
        if used + c > available:
            break
        used += c
        kept += 1
    kept_history = list(history[len(history) - kept :]) if kept else []
    return BudgetedPrompt(
        messages=[*head, *kept_history, *tail],
        prompt_tokens=used,
        history_kept=kept,
        history_dropped=len(history) - kept,
    )

"""Prompt token budget: history gives way, the bundle never does (plan assess-llm-runner, A)."""

from __future__ import annotations

import pytest
from test_context_bundle import _frozen
from test_context_snapshot import make

from vektra_assess.context.assemble import assemble_context
from vektra_assess.llm.budget import MESSAGE_OVERHEAD, ContextOverflowError, fit
from vektra_assess.llm.prompts import PromptRenderer
from vektra_assess.llm.types import PriorTurn
from vektra_shared.types import Message


def words(text: str) -> int:
    return len(text.split())


def _m(role: str, n: int, tag: str = "w") -> Message:
    return Message(role=role, content=" ".join([tag] * n))


HEAD = [_m("system", 10), _m("user", 20)]
TAIL = [_m("user", 5)]
FIXED = 35 + 3 * MESSAGE_OVERHEAD


def test_everything_fits() -> None:
    hist = [_m("user", 3, "a"), _m("assistant", 3, "b")]
    out = fit(HEAD, hist, TAIL, count=words, context_window=1000, max_tokens=100)
    assert out.messages == [*HEAD, *hist, *TAIL]
    assert (out.history_kept, out.history_dropped) == (2, 0)
    assert out.prompt_tokens == FIXED + 6 + 2 * MESSAGE_OVERHEAD


def test_history_is_trimmed_oldest_first() -> None:
    hist = [_m("user", 10, f"h{i}") for i in range(5)]
    per = 10 + MESSAGE_OVERHEAD
    window = FIXED + 2 * per + 50  # room for exactly two history messages
    out = fit(HEAD, hist, TAIL, count=words, context_window=window, max_tokens=50)
    assert out.messages == [*HEAD, hist[3], hist[4], *TAIL]
    assert (out.history_kept, out.history_dropped) == (2, 3)
    assert out.prompt_tokens <= window - 50


def test_the_completion_reserve_is_honoured() -> None:
    hist = [_m("user", 10)]
    window = FIXED + 10 + MESSAGE_OVERHEAD
    # the history message would fit the window, but not once max_tokens is reserved
    out = fit(HEAD, hist, TAIL, count=words, context_window=window, max_tokens=1)
    assert out.history_kept == 0


def test_a_bundle_that_does_not_fit_raises_and_is_never_cut() -> None:
    with pytest.raises(ContextOverflowError) as e:
        fit(HEAD, [], TAIL, count=words, context_window=FIXED + 9, max_tokens=10)
    assert (e.value.needed, e.value.available) == (FIXED, FIXED - 1)


def test_works_on_the_renderer_parts() -> None:
    s = make()
    b = assemble_context(s, _frozen(s))
    head, past, tail = PromptRenderer("x").turn_parts(
        b,
        (PriorTurn("student", "vecchio"), PriorTurn("assistant", "ok")),
        "nuovo",
        "it",
    )
    full = fit(head, past, tail, count=words, context_window=100_000, max_tokens=500)
    assert full.history_kept == 2
    tight = fit(
        head,
        past,
        tail,
        count=words,
        context_window=full.prompt_tokens - 1 + 500,
        max_tokens=500,
    )
    assert tight.history_kept == 1 and tight.messages[2] == past[1]  # the newest stays

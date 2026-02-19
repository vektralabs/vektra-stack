"""Token budget allocator for RAG prompt construction (ARCH-055).

Priority order (ARCH-055):
    system tokens + question tokens + reserve_tokens = fixed cost
    remaining * chunk_ratio   → chunk budget
    remaining * (1-chunk_ratio) → history budget

Chunk selection: highest-scoring chunks first (trim lowest score first).
History selection: most-recent turns first (trim oldest turns first).
"""
from __future__ import annotations


def allocate_token_budget(
    *,
    context_window: int,
    system_tokens: int,
    question_tokens: int,
    chunks: list[tuple[float, int]],
    history_turns: list[int],
    reserve: int = 1024,
    chunk_ratio: float = 0.6,
) -> tuple[list[int], list[int]]:
    """Select which chunks and history turns fit within the token budget.

    Args:
        context_window:  Total context window in tokens for the target model.
        system_tokens:   Token count of the rendered system prompt.
        question_tokens: Token count of the user question.
        chunks:          List of (score, token_count) sorted by score descending.
                         The allocator selects from the front (highest score) until
                         the chunk budget is exhausted.
        history_turns:   Token count per conversation turn, ordered oldest to newest.
                         The allocator selects from the end (newest) until
                         the history budget is exhausted.
        reserve:         Tokens reserved for the LLM's response generation.
        chunk_ratio:     Fraction of the remaining budget allocated to chunks.

    Returns:
        (selected_chunk_indices, selected_history_indices):
        - selected_chunk_indices: sorted ascending (same order as input `chunks`)
        - selected_history_indices: sorted ascending (same order as input `history_turns`)
    """
    fixed = system_tokens + question_tokens + reserve
    remaining = max(0, context_window - fixed)
    chunk_budget = int(remaining * chunk_ratio)
    history_budget = remaining - chunk_budget

    # Select chunks from highest score (front of list) until budget exhausted
    selected_chunks: list[int] = []
    used_chunk = 0
    for i, (_, tokens) in enumerate(chunks):
        if used_chunk + tokens <= chunk_budget:
            selected_chunks.append(i)
            used_chunk += tokens

    # Select history turns from newest (end of list) until budget exhausted
    selected_history: list[int] = []
    used_history = 0
    for i in range(len(history_turns) - 1, -1, -1):
        tokens = history_turns[i]
        if used_history + tokens <= history_budget:
            selected_history.insert(0, i)
            used_history += tokens

    return selected_chunks, selected_history

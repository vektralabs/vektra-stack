"""Unit tests for token budget allocator (ARCH-055)."""
import pytest
from vektra_core.budget import allocate_token_budget


def test_basic_allocation_all_fit():
    """All chunks and history fit within budget."""
    chunks_idx, history_idx = allocate_token_budget(
        context_window=4096,
        system_tokens=100,
        question_tokens=50,
        chunks=[(0.9, 200), (0.7, 200), (0.5, 200)],
        history_turns=[100, 100],
        reserve=512,
    )
    # chunk_budget = int((4096 - 100 - 50 - 512) * 0.6) = int(3434 * 0.6) = 2060
    # history_budget = 3434 - 2060 = 1374
    assert chunks_idx == [0, 1, 2]
    assert history_idx == [0, 1]


def test_chunk_budget_trims_lowest_score():
    """When chunk budget is tight, lowest-score chunks are dropped."""
    chunks_idx, _ = allocate_token_budget(
        context_window=1000,
        system_tokens=100,
        question_tokens=50,
        chunks=[(0.9, 300), (0.7, 300), (0.5, 300)],  # sorted by score desc
        history_turns=[],
        reserve=200,
        chunk_ratio=0.6,
    )
    # remaining = 1000 - 100 - 50 - 200 = 650; chunk_budget = int(650*0.6) = 390
    # chunk[0]=300 fits (used=300), chunk[1]=300 doesn't fit (300+300=600>390)
    assert chunks_idx == [0]


def test_history_budget_trims_oldest():
    """When history budget is tight, oldest turns are dropped."""
    _, history_idx = allocate_token_budget(
        context_window=1000,
        system_tokens=100,
        question_tokens=50,
        chunks=[],
        history_turns=[200, 150, 100],  # oldest to newest
        reserve=200,
        chunk_ratio=0.6,
    )
    # remaining=650; chunk_budget=390; history_budget=260
    # from newest: turn[2]=100 fits (used=100), turn[1]=150 fits (used=250),
    # turn[0]=200 doesn't fit (250+200=450>260)
    assert history_idx == [1, 2]


def test_no_budget_remaining():
    """When fixed costs exceed context window, nothing is selected."""
    chunks_idx, history_idx = allocate_token_budget(
        context_window=200,
        system_tokens=100,
        question_tokens=200,
        chunks=[(0.9, 10)],
        history_turns=[10],
        reserve=50,
    )
    # remaining = max(0, 200-350) = 0
    assert chunks_idx == []
    assert history_idx == []


def test_empty_inputs():
    """Empty chunks and history return empty selections."""
    chunks_idx, history_idx = allocate_token_budget(
        context_window=4096,
        system_tokens=100,
        question_tokens=50,
        chunks=[],
        history_turns=[],
        reserve=512,
    )
    assert chunks_idx == []
    assert history_idx == []


def test_history_selection_order_preserved():
    """Selected history indices are in ascending (chronological) order."""
    _, history_idx = allocate_token_budget(
        context_window=4096,
        system_tokens=50,
        question_tokens=50,
        chunks=[],
        history_turns=[100, 100, 100, 100],
        reserve=200,
    )
    assert history_idx == sorted(history_idx)

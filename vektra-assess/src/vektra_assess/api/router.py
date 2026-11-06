"""The assess API router. ``vektra_app.create_app`` mounts it only when assess is enabled.

Every route must depend on ``require_exact_scope`` or the assess token verifier
(``vektra-app/tests/test_assess_route_scopes.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from vektra_assess.api import (
    events,
    finish,
    housekeeping,
    mint,
    model_activations,
    sessions,
    setup_check,
    start,
    state,
    turns,
)

PREFIX = "/api/v1/assess"

router = APIRouter(
    prefix=PREFIX,
    tags=["assess"],
    # in-memory sweeps on request (no lifespan, BUG-025): DEKs, event channels, limiter keys
    dependencies=[Depends(housekeeping.assess_housekeeping)],
)
router.include_router(setup_check.router)
router.include_router(model_activations.router)
router.include_router(mint.router)
router.include_router(start.router)
router.include_router(sessions.router)
router.include_router(events.router)
router.include_router(turns.router)
router.include_router(state.router)
router.include_router(finish.router)

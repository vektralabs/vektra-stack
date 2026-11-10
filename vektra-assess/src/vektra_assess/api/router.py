"""The assess API router. ``vektra_app.create_app`` mounts it only when assess is enabled.

Every route must depend on ``require_exact_scope`` or the assess token verifier
(``vektra-app/tests/test_assess_route_scopes.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from vektra_assess.api import (
    events,
    finish,
    flag,
    housekeeping,
    mint,
    model_activations,
    ops,
    privacy,
    retry,
    sessions,
    setup_check,
    start,
    state,
    teacher_config,
    teacher_ops,
    teacher_sessions,
    transcripts,
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
router.include_router(retry.router)
router.include_router(flag.router)
router.include_router(transcripts.router)
# plan assess-teacher-ops: the activity configuration (editors only, write-only secrets)
router.include_router(teacher_config.router)
# plan assess-teacher-ops 11b: the session list and the operations (graders only)
router.include_router(teacher_sessions.router)
router.include_router(teacher_ops.router)
# plan assess-privacy-retention: privacy settings (operator), subject export and erasure
router.include_router(privacy.router)
router.include_router(ops.router)  # plan 12b: the operator's SMTP test send

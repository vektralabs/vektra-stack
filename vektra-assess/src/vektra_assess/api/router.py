"""The assess API router. ``vektra_app.create_app`` mounts it only when assess is enabled.

Every route must depend on ``require_exact_scope`` or the assess token verifier
(``vektra-app/tests/test_assess_route_scopes.py``).
"""

from __future__ import annotations

from fastapi import APIRouter

from vektra_assess.api import mint, model_activations, setup_check, start

PREFIX = "/api/v1/assess"

router = APIRouter(prefix=PREFIX, tags=["assess"])
router.include_router(setup_check.router)
router.include_router(model_activations.router)
router.include_router(mint.router)
router.include_router(start.router)

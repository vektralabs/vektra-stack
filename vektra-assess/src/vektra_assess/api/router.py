"""The assess API router. ``vektra_app.create_app`` mounts it only when assess is enabled.

Routes are added by later plans (session lifecycle, teacher ops, privacy). Every one of
them must depend on ``require_exact_scope`` or the assess token verifier
(``vektra-app/tests/test_assess_route_scopes.py``).
"""

from __future__ import annotations

from fastapi import APIRouter

PREFIX = "/api/v1/assess"

router = APIRouter(prefix=PREFIX, tags=["assess"])

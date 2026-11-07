"""NFR-023 activation endpoint (plan assess-llm-runner, task E).

``POST /api/v1/assess/admin/model-activations`` (exact ``admin`` scope) takes the JSON report of
``make eval-assess``, RECOMPUTES its verdict (``evaluate_report``: hash, HARD classes, judge
calibration, data basis) and appends an activation row only when it passes. Every attempt is
audited, accepted or refused, with content-free codes. The report's own ``status`` is never
trusted.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from vektra_assess.errors import activation_refused, assess_unavailable
from vektra_assess.llm.activation import evaluate_report
from vektra_shared.auth import ApiKeyInfo, require_exact_scope
from vektra_shared.errors import ErrorResponse, http_status_for

router = APIRouter(prefix="/admin/model-activations")
_guard = require_exact_scope("admin")

_AUDIT = text(
    "INSERT INTO assess_audit_events (event, reason_code, actor_kind, data) "
    "VALUES (:event, :reason, 'api', CAST(:data AS jsonb))"
)


def _fail(err: ErrorResponse) -> HTTPException:
    return HTTPException(status_code=http_status_for(err), detail=err.to_envelope())


def _engine(request: Request) -> AsyncEngine:
    engine = getattr(request.app.state, "assess_engine", None)
    if engine is None:
        raise _fail(assess_unavailable("assess_database"))
    return engine  # type: ignore[no-any-return]


@router.post("", status_code=201)
async def activate_model(
    request: Request,
    report: dict[str, Any] = Body(...),
    key: ApiKeyInfo = Depends(_guard),
) -> JSONResponse:
    ev = evaluate_report(report)
    data = {
        "model": ev.model[:200],
        "prompt_version": ev.prompt_version[:8],
        "report_sha256": ev.sha256[:64],
        "key_id": str(key.key_id),
    }
    async with _engine(request).begin() as conn:
        await conn.execute(
            _AUDIT,
            {
                "event": "assess_model_activation_accepted"
                if ev.accepted
                else "assess_model_activation_refused",
                "reason": ",".join(ev.reasons)[:200] or None,
                "data": json.dumps(data, sort_keys=True),
            },
        )
        if not ev.accepted:
            refused = activation_refused(ev.reasons)
        else:
            refused = None
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO assess_model_activations (provider, model, prompt_version, "
                        "report_sha256, hard_pass, activated_by_key_id) VALUES "
                        "(:p, :m, :v, :s, true, :k) RETURNING id, activated_at"
                    ),
                    {
                        "p": ev.provider,
                        "m": ev.model,
                        "v": ev.prompt_version,
                        "s": ev.sha256,
                        "k": key.key_id,
                    },
                )
            ).one()
    if refused is not None:
        raise _fail(refused)  # after commit: the refusal stays audited
    return JSONResponse(
        status_code=201,
        content={
            "id": row.id,
            "model": ev.model,
            "prompt_version": ev.prompt_version,
            "report_sha256": ev.sha256,
            "activated_at": row.activated_at.isoformat(),
        },
    )

"""``POST /api/v1/assess/tokens``: mint a pre-session token (ARCH-067, REQ-084).

Only a site's own ``assess:mint`` key may call it (``require_exact_scope``: the admin superscope
does not apply), and the site is the one that key is bound to in ``assess_site_keys`` (purpose
``mint``): the body never names a site. Checks, in order:

1. the body is well formed (400 otherwise, never 422);
2. the key is bound to an enabled site (403 ``ERR-ASSESS-007``);
3. the POSITIVE login-as assertion: both ``loginas: false`` and ``realuser_id: null`` present and
   exactly so. Anything else (absent, ``true``, ``0``, a user id) is audited
   ``assess_loginas_rejected`` and refused with 403; no token row is written;
4. ``privacy_notice_version``, when present (plugin request P3), has the stored
   ``notice_version``'s format (400 ``ERR-ASSESS-014`` ``<code>:privacy_notice_version``
   otherwise). It is NOT compared here: the same token may serve ``GET /state`` or a resume,
   which ignore it. It travels in the token as the claim ``notice_version``, and Start compares
   it with the site's current version (409 ``ERR-ASSESS-029`` ``privacy_notice_changed``);
5. ``plugin_version`` at least the configured minimum and the site's own (403 ``ERR-ASSESS-016``).

The mint row (``assess_token_jti``) and the ``assess_token_mint`` audit row are written in one
transaction; the response is the token and its lifetime only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from vektra_assess.api.common import SITE_OF_KEY, engine_of, fail, parse_body
from vektra_assess.api.deps import settings_of, tokens_of, version_at_least
from vektra_assess.api.models import MintRequest
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.errors import (
    bad_request,
    key_not_bound,
    loginas_refused,
    plugin_too_old,
)
from vektra_shared.auth import ApiKeyInfo, require_exact_scope

router = APIRouter()
_guard = require_exact_scope("assess:mint")

_INSERT_JTI = text(
    "INSERT INTO assess_token_jti (jti, site_id, student_userid, cmid, courseid_hint, "
    "mint_key_id, expires_at) VALUES (:jti, :site, :sub, :cmid, :course, :key, :exp)"
)


@router.post("/tokens", status_code=201)
async def mint_token(
    request: Request, key: ApiKeyInfo = Depends(_guard)
) -> JSONResponse:
    body = await parse_body(request, MintRequest)
    settings = settings_of(request)
    async with engine_of(request).begin() as conn:
        site = (await conn.execute(SITE_OF_KEY, {"k": key.key_id, "p": "mint"})).first()
        if site is None:
            raise fail(key_not_bound())
        site_id = site.id
        if not body.asserts_not_loginas():
            await write_event(
                conn,
                AssessAuditEvent.LOGINAS_REJECTED,
                actor="api",
                site_id=site_id,
                reason_code="mint_assertion_missing_or_false",
                data={"cmid": body.cmid, "key_id": key.key_id},
            )
            refused: Any = loginas_refused()
        elif (problem := body.notice_version_problem()) is not None:
            refused = bad_request(problem)
        elif not version_at_least(
            body.plugin_version, settings.min_plugin_version, site.min_plugin_version
        ):
            refused = plugin_too_old()
        else:
            refused = None
            minted = tokens_of(request).mint_presession(
                sub=body.sub,
                site=site_id,
                cmid=body.cmid,
                notice_version=body.privacy_notice_version,
            )
            await conn.execute(
                _INSERT_JTI,
                {
                    "jti": minted.jti,
                    "site": site_id,
                    "sub": body.sub,
                    "cmid": body.cmid,
                    "course": body.courseid,
                    "key": key.key_id,
                    "exp": minted.expires_at,
                },
            )
            await write_event(
                conn,
                AssessAuditEvent.TOKEN_MINT,
                actor="api",
                site_id=site_id,
                data={"cmid": body.cmid, "key_id": key.key_id},
            )
    if refused is not None:
        raise fail(refused)  # after commit: the login-as refusal stays audited
    return JSONResponse(
        status_code=201,
        content={"token": minted.token, "expires_in": minted.ttl_s},
        headers={"Cache-Control": "no-store"},
    )

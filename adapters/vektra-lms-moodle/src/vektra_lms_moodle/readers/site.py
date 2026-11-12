"""Reader ``site_info`` and the release-branch dispatch it feeds (plan group C)."""

from __future__ import annotations

import dataclasses

from pydantic import ValidationError

from vektra_lms_moodle.client import MoodleWsClient, SiteRef
from vektra_lms_moodle.domain import SiteInfo
from vektra_lms_moodle.models import SUPPORTED_BRANCHES, branch_of
from vektra_lms_moodle.readers._support import fetch, reader
from vektra_lms_moodle.result import Readable, Reading, Unreadable, UnreadableReason

WSFUNCTION = "core_webservice_get_site_info"


@reader
async def read_site_info(client: MoodleWsClient, site: SiteRef) -> Reading[SiteInfo]:
    """The release, the service account identity and the function list.

    The model set is chosen from the response's OWN ``release`` (the caller's ``SiteRef.release``
    may be stale or unset). ``userprivateaccesskey`` is parsed but never copied into the domain
    value, so it cannot reach a log, a trace or a stored snapshot (spike trap 5).
    """
    raw = await client.call(site, WSFUNCTION, {})
    if isinstance(raw, Unreadable):
        return raw
    doc = raw.value
    if isinstance(doc, dict) and "sitesecret" in doc:
        # Moodle 5.x adds ``sitesecret`` to the answer for a SITE ADMIN only. It is a secret:
        # dropped here, before parsing, so it is never kept, logged or stored. The strict model
        # would otherwise call the whole answer ``unknown_shape`` and hide the very fact the
        # setup check must report (``userissiteadmin``).
        doc = {k: v for k, v in doc.items() if k != "sitesecret"}
    release = doc.get("release") if isinstance(doc, dict) else None
    branch = branch_of(release if isinstance(release, str) else None)
    if branch is None:
        return Unreadable(UnreadableReason.VERSION_UNSUPPORTED, "unknown_branch")
    try:
        model = SUPPORTED_BRANCHES[branch][WSFUNCTION].validate_python(doc)
    except ValidationError:
        return Unreadable(UnreadableReason.UNKNOWN_SHAPE, "shape_mismatch")
    return Readable(
        SiteInfo(
            release=model.release,
            version=model.version,
            branch=branch,
            userid=model.userid,
            username=model.username,
            user_is_site_admin=model.userissiteadmin,
            functions=tuple(f.name for f in model.functions),
            downloadfiles=bool(model.downloadfiles),
        ),
        raw.evidence,
    )


def with_release(site: SiteRef, info: SiteInfo) -> SiteRef:
    """The ``SiteRef`` the other readers need: same site, release taken from ``site_info``."""
    return dataclasses.replace(site, release=info.release)


@reader
async def read_own_auth(
    client: MoodleWsClient, site: SiteRef, userid: int
) -> Reading[str]:
    """The ``auth`` plugin of the service account's OWN record (REQ-079, interactive login).

    ``core_user_get_users_by_field(id, self)`` answers ``webservice`` for an account that
    cannot log in interactively (spike: ``nologin`` is refused by the WS layer itself, so
    ``webservice`` is the only verifiable shape). The answer must be exactly the asked record;
    anything else is ``Unreadable``. Nothing else of the user record leaves this reader.
    """
    res = await fetch(
        client,
        site,
        "core_user_get_users_by_field",
        {"field": "id", "values": [userid]},
    )
    if isinstance(res, Unreadable):
        return res
    if len(res.value) != 1 or res.value[0].id != userid:
        return Unreadable(UnreadableReason.PARTIAL, "own_record_not_returned")
    return Readable(res.value[0].auth, res.evidence)

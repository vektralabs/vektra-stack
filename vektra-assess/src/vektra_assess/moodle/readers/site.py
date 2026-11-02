"""Reader ``site_info`` and the release-branch dispatch it feeds (plan group C)."""

from __future__ import annotations

import dataclasses

from pydantic import ValidationError

from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.domain import SiteInfo
from vektra_assess.moodle.models import SUPPORTED_BRANCHES, branch_of
from vektra_assess.moodle.readers._support import reader
from vektra_assess.moodle.result import Readable, Reading, Unreadable, UnreadableReason

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

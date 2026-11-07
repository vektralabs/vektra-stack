"""Transcript recipients as plan 11's config PUT will store them (plan 10b test support).

The teacher config is sealed with the worker's X25519 PUBLIC key by the API role (it may write the
column, never read it back) under the binding field ``config``; the worker opens it with the
private key through ``ConfigRecipients``.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from vektra_assess.crypto.seal import public_from_private
from vektra_assess.crypto.teacher_config import seal_teacher_config
from vektra_assess.delivery.recipients import (
    CONFIG_FIELD,
    RECIPIENTS_KEY,
    ConfigRecipients,
    recipient_ref,
)
from vektra_assess.keys import AssessKeyring

TEACHERS = ("prof.rossi@unical.it", "m.bianchi@unical.it")


def config_pair(kid: str = "w1") -> tuple[AssessKeyring, AssessKeyring]:
    raw = os.urandom(32)
    return (
        AssessKeyring(
            "assess_teacher_config_public", kid, {kid: public_from_private(raw)}
        ),
        AssessKeyring("assess_teacher_config_private", kid, {kid: raw}),
    )


async def configure_recipients(
    pg: Any,
    site: uuid.UUID,
    cmid: int,
    addresses: list[Any] | tuple[Any, ...] = TEACHERS,
    *,
    pair: tuple[AssessKeyring, AssessKeyring] | None = None,
    extra: dict[str, Any] | None = None,
) -> ConfigRecipients:
    """Seal ``{"transcript_recipients": addresses, **extra}`` into the activity's config row
    (which must exist) as the API role; return the worker-side reader."""
    pub, priv = pair or config_pair()
    doc = {RECIPIENTS_KEY: list(addresses), **(extra or {})}
    blob, kid = seal_teacher_config(pub, site, cmid, CONFIG_FIELD, json.dumps(doc))
    await pg.run(
        "api",
        "UPDATE assess_activity_config SET teacher_config_sealed = :b, "
        "teacher_config_kid = :k WHERE site_id = :s AND cmid = :c",
        {"b": blob, "k": kid, "s": site, "c": cmid},
    )
    return ConfigRecipients(priv)


def refs(
    site: uuid.UUID, addresses: tuple[str, ...] | list[str] = TEACHERS
) -> set[str]:
    return {recipient_ref(site, a) for a in addresses}

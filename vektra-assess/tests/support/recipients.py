"""Transcript recipients as plan 11's config PUT stores them (plan 10b test support).

Each teacher-config field is sealed with the worker's X25519 PUBLIC key by the API role (it may
write the row, never read the blob back) under its own binding ``[site, cmid, field]`` (revision
0036); the worker opens it with the private key through ``ConfigRecipients``.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from vektra_assess.crypto.seal import public_from_private
from vektra_assess.crypto.teacher_config import (
    PASTED_RUBRIC,
    SOLUTION,
    TRANSCRIPT_RECIPIENTS,
    store_sealed_field,
)
from vektra_assess.delivery.recipients import ConfigRecipients, recipient_ref
from vektra_assess.keys import AssessKeyring

TEACHERS = ("prof.rossi@uni.example", "m.bianchi@uni.example")


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
    """Seal ``addresses`` as the activity's ``transcript_recipients`` field (the config row must
    exist) as the API role, plus any ``extra`` text field (``solution``, ``pasted_rubric``);
    return the worker-side reader. The addresses are sealed as given (a test may plant bad ones);
    the refs are those of the strings."""
    pub, priv = pair or config_pair()
    items = [a for a in addresses if isinstance(a, str)]
    # bound to the site, as the teacher-config route is (RLS for the API role, ADR-0033 D9.2)
    async with pg.conn("api", site=site) as conn:
        await store_sealed_field(
            conn,
            pub,
            site,
            cmid,
            TRANSCRIPT_RECIPIENTS,
            json.dumps(list(addresses)),
            actor_userid=1,
            refs=[recipient_ref(site, a) for a in items][:3] or None,
            item_count=min(len(items), 3) or None,
        )
        for field in (SOLUTION, PASTED_RUBRIC):
            if extra and field in extra:
                await store_sealed_field(
                    conn, pub, site, cmid, field, extra[field], actor_userid=1
                )
        await conn.commit()
    return ConfigRecipients(priv)


def refs(
    site: uuid.UUID, addresses: tuple[str, ...] | list[str] = TEACHERS
) -> set[str]:
    return {recipient_ref(site, a) for a in addresses}

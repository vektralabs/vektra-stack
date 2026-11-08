"""The sealed teacher config: one row per field, each under its own binding (revision 0036).

``assess_activity_config_sealed`` holds ``solution``, ``pasted_rubric`` and
``transcript_recipients`` as separate rows sealed to the worker under ``[site, cmid, field]``.
The snapshot capture opens the solution (toggle ON only) and the pasted rubric (source "pasted"
only), the delivery opens the recipients, and a rotation keeps all three readable.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.recipients import TEACHERS, config_pair, configure_recipients
from support.world import configure_activity, make_site

from vektra_assess.crypto import rotation
from vektra_assess.delivery.recipients import ConfigRecipients
from vektra_assess.worker.preflight import StartTarget
from vektra_assess.worker.start_source import MoodleStartSource

pytestmark = pytest.mark.integration

SOLUTION = "La soluzione del docente: 42"
RUBRIC = "Criterio 1: chiarezza"


def _target(
    site: uuid.UUID, cmid: int, show: bool, source: str = "none"
) -> StartTarget:
    return StartTarget(
        session_id=uuid.uuid4(),
        site_id=site,
        cmid=cmid,
        student_userid=7,
        courseid_hint=None,
        activity_type="assign",
        session_no=1,
        flags={"show_solution": show, "rubric_source": source},
    )


async def test_capture_and_delivery_read_their_own_sealed_fields(pg) -> None:  # type: ignore[no-untyped-def]
    engine = create_async_engine(pg.url("worker"))
    try:
        site = await make_site(pg)
        await configure_activity(pg, site, 11)
        pub, priv = config_pair()
        reader = await configure_recipients(
            pg,
            site,
            11,
            pair=(pub, priv),
            extra={"solution": SOLUTION, "pasted_rubric": RUBRIC},
        )
        src = MoodleStartSource(None, None, engine.connect, teacher_private=priv)  # type: ignore[arg-type]
        got = await src._context(_target(site, 11, show=True, source="pasted"))
        assert got is not None and (got[2], got[3]) == (SOLUTION, RUBRIC)
        hidden = await src._context(_target(site, 11, show=False, source="moodle"))
        assert hidden is not None and (hidden[2], hidden[3]) == (
            None,
            None,
        )  # toggles rule
        async with engine.connect() as conn:
            assert await reader.read(conn, site, 11) == TEACHERS
        # a rotation re-seals every field under its own binding: every reader still opens it
        new_pub, new_priv = config_pair("w2")
        async with engine.begin() as conn:
            assert await rotation.reseal_teacher_configs(conn, priv, new_pub) == 0
        src2 = MoodleStartSource(None, None, engine.connect, teacher_private=new_priv)  # type: ignore[arg-type]
        got = await src2._context(_target(site, 11, show=True, source="pasted"))
        assert got is not None and (got[2], got[3]) == (SOLUTION, RUBRIC)
        async with engine.connect() as conn:
            assert await ConfigRecipients(new_priv).read(conn, site, 11) == TEACHERS
    finally:
        await engine.dispose()


async def test_a_config_without_a_solution_reads_none(pg) -> None:  # type: ignore[no-untyped-def]
    engine = create_async_engine(pg.url("worker"))
    try:
        site = await make_site(pg)
        await configure_activity(pg, site, 12)
        pub, priv = config_pair()
        await configure_recipients(pg, site, 12, pair=(pub, priv))
        src = MoodleStartSource(None, None, engine.connect, teacher_private=priv)  # type: ignore[arg-type]
        got = await src._context(_target(site, 12, show=True, source="pasted"))
        assert got is not None and (got[2], got[3]) == (None, None)
    finally:
        await engine.dispose()


async def test_a_cleared_recipients_field_reads_as_unreadable(pg) -> None:  # type: ignore[no-untyped-def]
    """Clearing keeps the row with no blob: the delivery reads None (unresolved), never ()."""
    engine = create_async_engine(pg.url("worker"))
    try:
        site = await make_site(pg)
        await configure_activity(pg, site, 13)
        pub, priv = config_pair()
        reader = await configure_recipients(pg, site, 13, pair=(pub, priv))
        from vektra_assess.crypto.teacher_config import (
            TRANSCRIPT_RECIPIENTS,
            store_sealed_field,
        )

        async with pg.conn("api") as conn:
            await store_sealed_field(
                conn, pub, site, 13, TRANSCRIPT_RECIPIENTS, None, actor_userid=2
            )
            await conn.commit()
        async with engine.connect() as conn:
            assert await reader.read(conn, site, 13) is None
    finally:
        await engine.dispose()

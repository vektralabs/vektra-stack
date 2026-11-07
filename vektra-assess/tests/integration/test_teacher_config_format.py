"""The sealed teacher config is ONE JSON object under binding ``config`` (plan 10b review #4).

``assess_activity_config.teacher_config_sealed`` holds ``{"solution"?: str,
"transcript_recipients": [...]}`` sealed to the worker under binding field ``config`` (the one the
rotation job re-seals under). Every reader opens it that way: the snapshot capture reads
``solution`` from it, the delivery reads ``transcript_recipients``, and a rotation keeps both.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.recipients import TEACHERS, config_pair, configure_recipients
from support.world import configure_activity, make_site

from vektra_assess.crypto import rotation
from vektra_assess.worker.preflight import StartTarget
from vektra_assess.worker.start_source import MoodleStartSource

pytestmark = pytest.mark.integration

SOLUTION = "La soluzione del docente: 42"


def _target(site: uuid.UUID, cmid: int, show: bool) -> StartTarget:
    return StartTarget(
        session_id=uuid.uuid4(),
        site_id=site,
        cmid=cmid,
        student_userid=7,
        courseid_hint=None,
        activity_type="assign",
        session_no=1,
        flags={"show_solution": show},
    )


async def test_capture_and_delivery_read_the_same_sealed_object(pg) -> None:  # type: ignore[no-untyped-def]
    engine = create_async_engine(pg.url("worker"))
    try:
        site = await make_site(pg)
        await configure_activity(pg, site, 11)
        pub, priv = config_pair()
        reader = await configure_recipients(
            pg, site, 11, pair=(pub, priv), extra={"solution": SOLUTION}
        )
        src = MoodleStartSource(None, None, engine.connect, teacher_private=priv)  # type: ignore[arg-type]
        got = await src._context(_target(site, 11, show=True))
        assert got is not None and got[2] == SOLUTION
        hidden = await src._context(_target(site, 11, show=False))
        assert hidden is not None and hidden[2] is None  # the toggle still rules
        async with engine.connect() as conn:
            assert await reader.read(conn, site, 11) == TEACHERS
        # a rotation re-seals under the same binding: both readers still open it
        new_pub, new_priv = config_pair("w2")
        async with engine.begin() as conn:
            assert await rotation.reseal_teacher_configs(conn, priv, new_pub) == 0
        src2 = MoodleStartSource(None, None, engine.connect, teacher_private=new_priv)  # type: ignore[arg-type]
        got = await src2._context(_target(site, 11, show=True))
        assert got is not None and got[2] == SOLUTION
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
        got = await src._context(_target(site, 12, show=True))
        assert got is not None and got[2] is None
    finally:
        await engine.dispose()

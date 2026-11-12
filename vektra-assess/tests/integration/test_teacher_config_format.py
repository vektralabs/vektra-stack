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
from vektra_assess.worker.start_source import LmsStartSource

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
        src = LmsStartSource(None, None, engine.connect, teacher_private=priv)  # type: ignore[arg-type]
        got = await src._context(_target(site, 11, show=True, source="pasted"))
        assert got is not None and (got[2], got[3]) == (SOLUTION, RUBRIC)
        hidden = await src._context(_target(site, 11, show=False, source="lms"))
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
        src2 = LmsStartSource(None, None, engine.connect, teacher_private=new_priv)  # type: ignore[arg-type]
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
        src = LmsStartSource(None, None, engine.connect, teacher_private=priv)  # type: ignore[arg-type]
        got = await src._context(_target(site, 12, show=True, source="pasted"))
        assert got is not None and (got[2], got[3]) == (None, None)
    finally:
        await engine.dispose()


async def test_unset_or_cleared_recipients_read_as_none_configured(pg) -> None:  # type: ignore[no-untyped-def]
    """Review 4: unset or cleared is a readable answer, ``()`` (generation records
    ``no_recipients``); only a field that does not open is ``None`` (``config_unreadable``)."""
    engine = create_async_engine(pg.url("worker"))
    try:
        site = await make_site(pg)
        await configure_activity(pg, site, 13)
        pub, priv = config_pair()
        reader = ConfigRecipients(priv)
        async with engine.connect() as conn:
            assert await reader.read(conn, site, 13) == ()  # never set
        await configure_recipients(pg, site, 13, pair=(pub, priv))
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
            assert await reader.read(conn, site, 13) == ()  # cleared
        # sealed to another key: unreadable
        await configure_recipients(pg, site, 13, pair=(config_pair("x8")[0], priv))
        async with engine.connect() as conn:
            assert await reader.read(conn, site, 13) is None
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("field", "flags"),
    [
        ("solution", {"show_solution": True, "rubric_source": "none"}),
        ("pasted_rubric", {"show_solution": False, "rubric_source": "pasted"}),
    ],
)
async def test_a_needed_field_that_does_not_open_fails_the_capture_closed(  # type: ignore[no-untyped-def]
    pg, field, flags
) -> None:
    """Review 1: a seal the worker cannot open (another key) for a field the activity needs
    rolls the Start back; it never starts without the solution or the pasted rubric."""
    from vektra_assess.context.capture.capture import Failed, FailReason

    engine = create_async_engine(pg.url("worker"))
    try:
        site = await make_site(pg)
        await configure_activity(pg, site, 14)
        other_pub, _ = config_pair("x9")
        _, priv = config_pair()
        await configure_recipients(
            pg, site, 14, pair=(other_pub, priv), extra={field: "testo"}
        )
        src = LmsStartSource(None, None, engine.connect, teacher_private=priv)  # type: ignore[arg-type]
        target = _target(
            site, 14, show=flags["show_solution"], source=flags["rubric_source"]
        )
        got = await src.capture(target)
        assert got == Failed(
            FailReason.POLICY_INPUT_UNREADABLE, "teacher_config_unreadable"
        )
    finally:
        await engine.dispose()


async def test_without_the_private_key_a_stored_needed_field_fails_closed(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4505511253: a worker built without the teacher-config key cannot open a stored
    solution; it must not start without it. An unset field keeps the old behaviour."""
    engine = create_async_engine(pg.url("worker"))
    try:
        site = await make_site(pg)
        await configure_activity(pg, site, 15)
        await configure_activity(pg, site, 16)
        pub, priv = config_pair()
        await configure_recipients(
            pg, site, 15, pair=(pub, priv), extra={"solution": "x"}
        )
        src = LmsStartSource(None, None, engine.connect, teacher_private=None)  # type: ignore[arg-type]
        stored = await src._context(_target(site, 15, show=True))
        assert stored is not None and stored[4] is True
        unset = await src._context(_target(site, 16, show=True))
        assert unset is not None and unset[4] is False and unset[2] is None
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("show", "source", "fetched"),
    [
        (False, "lms", None),
        (False, "none", None),
        (True, "none", ["solution"]),
        (False, "pasted", ["pasted_rubric"]),
        (True, "pasted", ["solution", "pasted_rubric"]),
    ],
)
async def test_capture_fetches_only_the_fields_it_may_see(  # type: ignore[no-untyped-def]
    pg, monkeypatch, show, source, fetched
) -> None:
    """CodeRabbit nitpick (PR 170): a hidden field is not fetched and opened then discarded; it
    is never asked for (the SQL field list is exactly the visible fields, or no read at all)."""
    from vektra_assess.worker import start_source

    calls: list[list[str]] = []
    real = start_source.read_teacher_config

    async def spy(conn, key, site_id, cmid, fields):  # type: ignore[no-untyped-def]
        calls.append(list(fields))
        return await real(conn, key, site_id, cmid, fields)

    monkeypatch.setattr(start_source, "read_teacher_config", spy)
    engine = create_async_engine(pg.url("worker"))
    try:
        site = await make_site(pg)
        await configure_activity(pg, site, 17)
        pub, priv = config_pair()
        await configure_recipients(
            pg,
            site,
            17,
            pair=(pub, priv),
            extra={"solution": SOLUTION, "pasted_rubric": RUBRIC},
        )
        src = LmsStartSource(None, None, engine.connect, teacher_private=priv)  # type: ignore[arg-type]
        got = await src._context(_target(site, 17, show=show, source=source))
        assert got is not None
        assert calls == ([] if fetched is None else [fetched])
        assert got[2] == (SOLUTION if show else None)
        assert got[3] == (RUBRIC if source == "pasted" else None)
    finally:
        await engine.dispose()

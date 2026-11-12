"""BR-015 on real Postgres: commit hash == recomputed hash, immutability, tamper, rollback.

Rows are counted as the superuser (no role but the worker can read snapshots, and no role can read
the audit log), never inferred from a function's return value.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from support.fake_lms import SCENARIO_RELEASE

from vektra_assess.context.capture.store import (
    Mismatch,
    MismatchReason,
    Ok,
    delete_snapshot,
    insert_snapshot,
    load_snapshot,
    verify_snapshot_integrity,
)
from vektra_assess.context.snapshot import canonical_bytes, content_hash

from .conftest import Pg, make_session

pytestmark = pytest.mark.integration

CANARY = "CNRY-submission-onlinetext-77aa11bb"


def _quiz(provider: Any) -> tuple[Any, Any]:
    """(the capture readers, the target) of the scenario world's graded essay quiz, read
    through ``provider`` bound to the scenario site (the fake LMS, ADR-0033)."""
    from support.fake_lms import QUIZZES, SCENARIO_COURSE, scenario_target

    from vektra_assess.context.capture.bound import SiteReaders
    from vektra_assess.context.capture.capture import QuizTarget
    from vektra_shared.lms import LmsSite

    sub, cmid = scenario_target("essay")
    readers = SiteReaders(
        provider, LmsSite("site-1", "https://lms.example.org", SCENARIO_RELEASE)
    )
    return readers, QuizTarget(SCENARIO_COURSE, QUIZZES["essay"][1], cmid, sub)


def _snapshot():  # type: ignore[no-untyped-def]
    from test_context_snapshot import make

    return make(CANARY)


async def _count(pg: Pg, sql: str, sid: object) -> int:
    rows = await pg.run("super", sql, {"s": sid})
    return int(rows[0][0])


async def _insert(pg: Pg) -> tuple[object, bytes, str]:
    sid = await make_session(pg, "STARTING")
    dek = os.urandom(32)
    async with pg.conn("worker") as c:
        digest = await insert_snapshot(c, sid, dek, _snapshot())
        await c.commit()
    return sid, dek, digest


async def test_committed_hash_equals_the_recomputed_hash(pg: Pg) -> None:
    sid, dek, digest = await _insert(pg)
    assert digest == content_hash(_snapshot())
    async with pg.conn("worker") as c:
        res = await verify_snapshot_integrity(c, sid, dek, digest)
        loaded = await load_snapshot(c, sid, dek)
        await c.commit()
    assert isinstance(res, Ok) and loaded == _snapshot()
    # the sealed snapshot is schema 2: neutral item kind and state (ADR-0033 D2)
    assert loaded.header.schema_version == 2
    # the stored bytes are ciphertext: the canary is not in the row
    rows = await pg.run(
        "super",
        "SELECT ciphertext FROM assess_snapshots WHERE session_id=:s",
        {"s": sid},
    )
    assert CANARY.encode() not in bytes(rows[0][0])
    assert canonical_bytes(_snapshot()) not in bytes(rows[0][0])
    assert (
        await _count(
            pg, "SELECT count(*) FROM assess_audit_events WHERE session_id=:s", sid
        )
        == 0
    )


async def test_a_forced_update_is_rejected(pg: Pg) -> None:
    sid, _, _ = await _insert(pg)
    with pytest.raises(DBAPIError) as exc:  # no grant for the worker
        await pg.run(
            "worker",
            "UPDATE assess_snapshots SET content_hash='x' WHERE session_id=:s",
            {"s": sid},
        )
    assert exc.value.orig.sqlstate == "42501"
    with pytest.raises(
        DBAPIError, match="immutable"
    ):  # the trigger stops even the owner
        await pg.run(
            "super",
            "UPDATE assess_snapshots SET content_hash='x' WHERE session_id=:s",
            {"s": sid},
        )


@pytest.mark.parametrize("what", ["ciphertext", "column", "both"])
async def test_tampering_is_a_mismatch_with_one_audit_row(pg: Pg, what: str) -> None:
    sid, dek, digest = await _insert(pg)
    # tamper around the trigger, as an attacker with table-owner rights would
    async with pg.conn("super") as c:
        await c.execute(text("SET session_replication_role = replica"))
        if what in ("ciphertext", "both"):
            await c.execute(
                text(
                    # XOR, not overwrite: a byte that already was 0xff (1 run in 256)
                    # would leave the ciphertext intact and the test would flake
                    "UPDATE assess_snapshots SET ciphertext = set_byte(ciphertext, "
                    "length(ciphertext) - 21, get_byte(ciphertext, length(ciphertext) - 21) # 255) "
                    "WHERE session_id=:s"
                ),
                {"s": sid},
            )
        if what in ("column", "both"):
            await c.execute(
                text(
                    "UPDATE assess_snapshots SET content_hash='forged' WHERE session_id=:s"
                ),
                {"s": sid},
            )
        await c.commit()
    async with pg.conn("worker") as c:
        res = await verify_snapshot_integrity(c, sid, dek, digest)
        await c.commit()
    expected = (
        MismatchReason.HASH_MISMATCH
        if what == "column"
        else MismatchReason.DECRYPT_FAILED
    )
    assert res == Mismatch(expected)
    rows = await pg.run(
        "super",
        "SELECT event, reason_code, data::text FROM assess_audit_events WHERE session_id=:s",
        {"s": sid},
    )
    assert [(r[0], r[1]) for r in rows] == [
        ("assess_snapshot_integrity_failed", expected.value)
    ]
    assert CANARY not in rows[0][2]


async def test_a_wrong_key_is_a_mismatch(pg: Pg) -> None:
    sid, _, digest = await _insert(pg)
    async with pg.conn("worker") as c:
        res = await verify_snapshot_integrity(c, sid, os.urandom(32), digest)
        await c.commit()
    assert res == Mismatch(MismatchReason.DECRYPT_FAILED)


async def test_rollback_leaves_zero_rows(pg: Pg) -> None:
    sid, _, _ = await _insert(pg)
    assert (
        await _count(
            pg, "SELECT count(*) FROM assess_snapshots WHERE session_id=:s", sid
        )
        == 1
    )
    async with pg.conn("worker") as c:
        assert await delete_snapshot(c, sid) == 1
        assert await delete_snapshot(c, sid) == 0
        await c.commit()
    assert (
        await _count(
            pg, "SELECT count(*) FROM assess_snapshots WHERE session_id=:s", sid
        )
        == 0
    )
    async with pg.conn("worker") as c:
        res = await verify_snapshot_integrity(c, sid, os.urandom(32), "h")
        await c.commit()
    assert res == Mismatch(MismatchReason.MISSING)


async def test_a_failed_audit_write_aborts_the_transaction(pg: Pg) -> None:
    """Fail closed: an exclusion event that cannot be written takes the snapshot with it."""
    from vektra_assess.context.capture.store import write_audit

    sid = await make_session(pg, "STARTING")
    async with pg.conn("worker") as c:
        await insert_snapshot(c, sid, os.urandom(32), _snapshot())
        with pytest.raises(DBAPIError):  # data over the 2 KiB cap
            await write_audit(
                c,
                "assess_context_exclusion_applied",
                session_id=sid,
                data={"x": "y" * 4000},
            )
        await c.rollback()
    assert (
        await _count(
            pg, "SELECT count(*) FROM assess_snapshots WHERE session_id=:s", sid
        )
        == 0
    )


async def test_persist_capture_writes_snapshot_and_exclusion_event_together(
    pg: Pg,
) -> None:
    from support.fake_lms import scenario_provider
    from test_context_capture import OFF, InProcessParser

    from vektra_assess.context.capture.capture import (
        Built,
        Capabilities,
        capture_quiz,
        persist_capture,
    )

    readers, target = _quiz(scenario_provider())
    built = await capture_quiz(
        target,
        readers,
        InProcessParser(),
        OFF,
        Capabilities(),
        lms_release=SCENARIO_RELEASE,
        now=1,
    )
    assert isinstance(built, Built)
    sid = await make_session(pg, "STARTING")
    dek = os.urandom(32)
    async with pg.conn("worker") as c:
        digest = await persist_capture(c, sid, dek, built)
        await c.commit()
    assert (
        await _count(
            pg, "SELECT count(*) FROM assess_snapshots WHERE session_id=:s", sid
        )
        == 1
    )
    rows = await pg.run(
        "super",
        "SELECT event, data FROM assess_audit_events WHERE session_id=:s ORDER BY id",
        {"s": sid},
    )
    assert [r[0] for r in rows] == ["assess_context_exclusion_applied"]
    data = rows[0][1]
    assert data["excluded_field_set_hash"] == built.snapshot.exclusion.field_set_hash()
    assert set(data) == {
        "policy_version",
        "excluded_field_set_hash",
        "html_structured_only",
        "rubric_descriptors_excluded",
        "solution_shown",
    }
    async with pg.conn("worker") as c:
        assert isinstance(await verify_snapshot_integrity(c, sid, dek, digest), Ok)
        await c.commit()


async def test_canary_matrix_subset_leaves_no_canary_in_any_row(pg: Pg) -> None:
    """Sink 6 on real Postgres: persist one case per kind and shape, then scan the audit rows and
    a data-only dump of every assess table for the forbidden canaries."""
    from canary.matrix import build_matrix
    from canary.runner import run_case

    from vektra_assess.context.capture.capture import persist_capture
    from vektra_shared.testing_recorders import find_canaries

    from .conftest import dump_data

    picked = {}
    for c in build_matrix():
        key = (c.kind, c.shape or c.review)
        if c.version == "5.2" and not c.toggle and key not in picked:
            picked[key] = c
    forbidden: set[str] = set()
    persisted = 0
    for case in picked.values():
        out = await run_case(case)
        forbidden |= set(case.forbidden)
        if out.built is None:
            continue
        sid = await make_session(pg, "STARTING")
        async with pg.conn("worker") as c:
            await persist_capture(c, sid, os.urandom(32), out.built)
            await c.commit()
        persisted += 1
    assert persisted >= 8 and forbidden
    rows = await pg.run("super", "SELECT data::text FROM assess_audit_events")
    assert rows, "the audit sink is not empty"
    assert sum(find_canaries([r[0] for r in rows], forbidden).values()) == 0
    dump = dump_data(pg)
    assert "assess_snapshots" in dump
    assert sum(find_canaries(dump, forbidden).values()) == 0


async def test_a_regrade_between_capture_and_close_changes_only_the_flag(
    pg: Pg,
) -> None:
    """BR-008 / BR-015 AC2: Moodle regrades after Start; the snapshot, its hash and Part A stay
    as captured, and the close-time comparison reports changed(old, new)."""
    from support.fake_lms import scenario_provider
    from test_context_capture import OFF, InProcessParser

    from vektra_assess.context.assemble import assemble_context
    from vektra_assess.context.capture.capture import (
        Built,
        Capabilities,
        capture_quiz,
        persist_capture,
    )
    from vektra_assess.context.part_a import SessionFacts, render_part_a
    from vektra_assess.context.regrade import (
        GradeReading,
        RegradeFlag,
        RegradeKind,
        compare_grade,
    )
    from vektra_assess.context.snapshot import mark_str
    from vektra_shared.lms import Readable

    fake = scenario_provider()
    readers, target = _quiz(fake)
    built = await capture_quiz(
        target,
        readers,
        InProcessParser(),
        OFF,
        Capabilities(),
        lms_release=SCENARIO_RELEASE,
        now=1,
    )
    assert isinstance(built, Built)
    sid, dek = await make_session(pg, "STARTING"), os.urandom(32)
    async with pg.conn("worker") as c:
        digest = await persist_capture(c, sid, dek, built)
        await c.commit()
    part_a_before = render_part_a(
        assemble_context(built.snapshot, built.frozen_policy), SessionFacts(1, 30)
    ).text

    # the teacher regrades in the LMS after Start
    key = (target.courseid, target.userid, target.cmid)
    fake.world.grade_items[key] = dataclasses.replace(
        fake.world.grade_items[key], grade=6.5, grade_formatted="6.50"
    )
    now = await readers.grade_item(target.courseid, target.userid, target.cmid)
    assert isinstance(now, Readable) and now.value.grade == 6.5
    flag = compare_grade(
        built.snapshot, Readable(GradeReading(True, mark_str(now.value.grade)))
    )
    assert flag == RegradeFlag(RegradeKind.CHANGED, old="8", new="6.5")
    async with pg.conn("worker") as c:
        res = await verify_snapshot_integrity(c, sid, dek, digest)
        await c.commit()
    assert isinstance(res, Ok) and res.snapshot == built.snapshot  # never refreshed
    part_a_after = render_part_a(
        assemble_context(res.snapshot, built.frozen_policy), SessionFacts(1, 30)
    ).text
    assert part_a_after == part_a_before

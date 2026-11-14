"""The editor check and the sealed-field writer without a database (plan assess-teacher-ops).

The integration tests run them against real Postgres with the restricted roles; these pin the
decision table and the statement parameters with a recording connection.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from vektra_assess.api.teachers import TeacherCheck, check_fresh_teacher
from vektra_assess.crypto import seal, teacher_config
from vektra_assess.keys import AssessKeyring

SITE = uuid.uuid4()


class Conn:
    def __init__(self, row: Any = None) -> None:
        self.row = row
        self.sql: list[tuple[str, Any]] = []

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self.sql.append((str(stmt), params))
        return SimpleNamespace(first=lambda: self.row, rowcount=1)


def gate(age: float = 1.0, **facts: Any) -> SimpleNamespace:
    inputs = {"resolved": True, "editors": [5], "hint": [7, "quiz"], **facts}
    return SimpleNamespace(inputs=inputs, age_s=age)


def check(
    conn: Conn, actor: int = 5, hint: tuple[Any, Any] = (7, "quiz")
) -> TeacherCheck:
    got = asyncio.run(
        check_fresh_teacher(
            conn,
            site=SITE,
            cmid=3,
            actor_userid=actor,
            max_age_s=120,
            courseid_hint=hint[0],
            kind_hint=hint[1],
            role="editor",
        )
    )
    return got.check


def queued(conn: Conn) -> list[Any]:
    return [p for sql, p in conn.sql if "INSERT INTO assess_jobs" in sql]


@pytest.mark.parametrize(
    ("row", "actor", "hint", "want", "jobs"),
    [
        (gate(), 5, (7, "quiz"), TeacherCheck.OK, 0),
        (gate(), 6, (7, "quiz"), TeacherCheck.NOT_AN_EDITOR, 0),
        (None, 5, (7, "quiz"), TeacherCheck.STALE, 1),
        (gate(age=121), 5, (7, "quiz"), TeacherCheck.STALE, 1),
        (gate(editors=None), 5, (7, "quiz"), TeacherCheck.STALE, 1),
        (gate(resolved=False, reason="activity_not_in_course"), 5, (7, "quiz"),
         TeacherCheck.NOT_AN_EDITOR, 0),
        (gate(resolved=False, reason="http_5xx"), 5, (7, "quiz"), TeacherCheck.STALE, 1),
        (gate(resolved=False, reason="activity_not_in_course"), 5, (8, "quiz"),
         TeacherCheck.STALE, 1),
        (None, 5, (None, None), TeacherCheck.HINT_MISSING, 0),
        (None, 5, (7, "forum"), TeacherCheck.HINT_MISSING, 0),
    ],
)  # fmt: skip
def test_the_editor_check_decision_table(
    row: Any, actor: int, hint: tuple[Any, Any], want: TeacherCheck, jobs: int
) -> None:
    conn = Conn(row)
    assert check(conn, actor, hint) is want
    sent = queued(conn)
    assert len(sent) == jobs
    for params in sent:
        assert json.loads(params["args"]) == {
            "site_id": str(SITE), "cmid": 3, "courseid": hint[0], "activity_type": hint[1],
        }  # fmt: skip


def test_the_sealed_writer_seals_per_field_and_never_sends_refs_for_a_clear() -> None:
    raw = os.urandom(32)
    pub = AssessKeyring("p", "w1", {"w1": seal.public_from_private(raw)})
    priv = AssessKeyring("q", "w1", {"w1": raw})
    conn = Conn()
    asyncio.run(
        teacher_config.store_sealed_field(
            conn, pub, SITE, 3, teacher_config.TRANSCRIPT_RECIPIENTS, '["a@x.it"]',
            actor_userid=9, refs=["b" * 64, "a" * 64], item_count=2,
        )
    )  # fmt: skip
    asyncio.run(
        teacher_config.store_sealed_field(
            conn,
            pub,
            SITE,
            3,
            teacher_config.SOLUTION,
            None,
            actor_userid=9,
            refs=["c" * 64],
        )
    )
    (_, put), (_, clear) = conn.sql
    assert put["r"] == ["a" * 64, "b" * 64] and put["n"] == 2 and put["k"] == "w1"
    opened = teacher_config.open_teacher_config(
        priv, put["b"], SITE, 3, teacher_config.TRANSCRIPT_RECIPIENTS
    )
    assert opened == '["a@x.it"]'
    assert (clear["b"], clear["k"], clear["r"], clear["n"]) == (None, None, None, None)
    with pytest.raises(ValueError, match="unknown sealed field"):
        asyncio.run(
            teacher_config.store_sealed_field(
                conn, pub, SITE, 3, "notes", "x", actor_userid=9
            )
        )


def test_one_fresh_check_entry_and_no_name_clash_with_the_transcript_grader_check() -> (
    None
):
    """Review 11b-9: the teacher routes use ``check_fresh_teacher`` (through
    ``verify_teacher``); no second ``check_fresh_grader`` shadows ``api.graders``'s per-student
    check, and no dead wrapper remains."""
    from vektra_assess.api import graders, teachers

    assert not hasattr(teachers, "check_fresh_grader")
    assert not hasattr(teachers, "check_fresh_editor")
    assert callable(graders.check_fresh_grader)


# --- freshness for the plugin (live E2E follow-up) ---------------------------------------------


def _checked(age: float) -> Any:
    return asyncio.run(
        check_fresh_teacher(
            Conn(gate(age=age)),
            site=SITE,
            cmid=3,
            actor_userid=5,
            max_age_s=120,
            courseid_hint=7,
            kind_hint="quiz",
            role="editor",
        )
    )


def test_the_gate_records_its_age_and_only_a_just_read_row_is_final() -> None:
    from vektra_assess.api.teachers import FACT_FRESH_S, gate_hint

    got = _checked(FACT_FRESH_S + 1)
    assert got.ok and got.age_s == FACT_FRESH_S + 1 and not got.just_read
    assert gate_hint(got) == (7, "quiz")
    assert _checked(1.0).just_read


def test_a_row_without_a_usable_hint_queues_no_refresh_after_a_fact_refusal() -> None:
    from vektra_assess.api.teachers import TeacherGate, refresh_after_fact_refusal

    class Engine:
        """Counts transactions: the helper swallows exceptions (a failed queue only loses the
        hint), so raising here could not prove that nothing was attempted."""

        def __init__(self) -> None:
            self.begun = 0

        def begin(self) -> Any:
            self.begun += 1
            raise RuntimeError("nothing may be queued")

    engine = Engine()
    for facts in ({}, {"hint": [7, "forum"]}, {"hint": [True, "quiz"]}, {"hint": "x"}):
        g = TeacherGate(TeacherCheck.OK, facts, age_s=60.0)
        assert asyncio.run(refresh_after_fact_refusal(engine, SITE, 3, g)) is False
    # just read: final, nothing queued either
    g = TeacherGate(TeacherCheck.OK, {"hint": [7, "quiz"]}, age_s=1.0)
    assert asyncio.run(refresh_after_fact_refusal(engine, SITE, 3, g)) is False
    assert engine.begun == 0
    # the control: a usable hint on old facts does open a transaction
    g = TeacherGate(TeacherCheck.OK, {"hint": [7, "quiz"]}, age_s=60.0)
    assert asyncio.run(refresh_after_fact_refusal(engine, SITE, 3, g)) is False
    assert engine.begun == 1


def test_the_retry_hint_is_in_one_place_capped_and_never_on_a_final_refusal() -> None:
    from vektra_assess.errors import (
        RETRY_AFTER_MAX_S,
        config_invalid,
        reset_refused,
        retry_details,
        retry_headers,
        teacher_unverified,
    )

    stale = teacher_unverified(stale=True, role="editor", retry_after_s=60)
    assert stale.details == {
        "reason": "editor_check_stale",
        "retry_after_s": RETRY_AFTER_MAX_S,
    }
    assert stale.retry_after is None  # not the envelope's top-level field
    assert retry_headers(stale) == {"Retry-After": str(RETRY_AFTER_MAX_S)}
    final = teacher_unverified(stale=False, role="editor")
    assert final.details == {"reason": "not_an_editor"} and retry_headers(final) is None
    assert config_invalid([], retry_after_s=0).details == {
        "fields": [],
        "retry_after_s": 1,
    }
    assert "retry_after_s" not in config_invalid([]).details
    assert reset_refused("gate_inputs_stale", **retry_details(3)).details == {
        "reason": "gate_inputs_stale",
        "retry_after_s": 3,
    }

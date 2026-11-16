"""The per-turn eligibility round trip on real Postgres (FEAT-028, ARCH-072 amendment).

The API reader (as ``vektra_assess_api``, RLS-bound to the site) queues the job and waits; the
worker handler (as ``vektra_assess_worker``) answers with one row; the reader takes the row of its
own request and session only, written after the turn began, and deletes it. Timeout, a job that
cannot be queued and an unreadable read are all "unreadable"; the sweeper removes late rows.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_assess.api import material_reads
from vektra_assess.api.material_reads import ApiEligibilityReader
from vektra_assess.context.materials import EligibleModule
from vektra_assess.worker.material import read_material, sweep_material_reads
from vektra_shared.lms import (
    ActivityRef,
    CourseModuleVisibility,
    Readable,
    Unreadable,
    UnreadableReason,
)

from .conftest import Pg, make_session

pytestmark = pytest.mark.integration

MODULES = (
    CourseModuleVisibility(11, "Lezione 1", 1, True, True, False, True, True),
    CourseModuleVisibility(12, "Soluzioni [no-ai]", 1, True, True, False, True, True),
    CourseModuleVisibility(13, "Nascosto", 1, False, True, False, True, True),
)


class Source:
    def __init__(self, reading: Any = None, tag: str | None = "[no-ai]") -> None:
        self.reading = reading if reading is not None else Readable(MODULES)
        self.rag_opt_out_tag = tag
        self.calls = 0

    async def course_material(self, target: Any) -> Any:
        self.calls += 1
        return ActivityRef(10, 5, "Psico Prova"), self.reading


async def _session(pg: Pg) -> tuple[uuid.UUID, uuid.UUID]:
    sid = await make_session(pg)
    site = (
        await pg.run(
            "super", "SELECT site_id FROM assess_sessions WHERE id = :s", {"s": sid}
        )
    )[0][0]
    await pg.run(
        "super",
        "INSERT INTO assess_activity_config (site_id, cmid, activity_type, rag_enabled, "
        "rag_attested_at, rag_attested_by) VALUES (:s, 1, 'quiz', true, now(), '7') "
        "ON CONFLICT DO NOTHING",
        {"s": site},
    )
    return sid, site


def _acquire(engine: Any) -> Any:
    @asynccontextmanager
    async def acquire():  # type: ignore[no-untyped-def]
        async with engine.connect() as conn:
            yield conn

    return acquire


async def _serve_one(pg: Pg, source: Source, worker: Any) -> None:
    """The worker side: take the queued material job and run its handler body."""
    for _ in range(100):
        rows = await pg.run(
            "super",
            "SELECT args FROM assess_jobs WHERE kind = 'material_eligibility' "
            "AND state = 'ready' ORDER BY id DESC LIMIT 1",
        )
        if rows:
            args = rows[0][0]
            await pg.run(
                "super",
                "UPDATE assess_jobs SET state = 'done' WHERE kind = 'material_eligibility' "
                "AND args->>'request_id' = :r",
                {"r": args["request_id"]},
            )
            await read_material(
                source,
                _acquire(worker),
                uuid.UUID(args["session_id"]),
                uuid.UUID(args["request_id"]),
            )
            return
        await asyncio.sleep(0.05)
    raise AssertionError("no material_eligibility job was queued")


async def _round_trip(pg: Pg, source: Source, timeout_s: float = 5.0):  # type: ignore[no-untyped-def]
    sid, site = await _session(pg)
    api = create_async_engine(pg.url("api"))
    worker = create_async_engine(pg.url("worker"))
    seen: list[tuple[str, float]] = []
    try:
        reader = ApiEligibilityReader(
            api, timeout_s=timeout_s, observe=lambda o, s: seen.append((o, s))
        )
        read, _ = await asyncio.gather(
            reader(sid, site), _serve_one(pg, source, worker)
        )
    finally:
        await api.dispose()
        await worker.dispose()
    return sid, read, seen


async def test_a_readable_read_gives_the_eligible_modules_and_is_deleted(
    pg: Pg,
) -> None:
    sid, read, seen = await _round_trip(pg, Source())
    assert read.readable and read.namespace == "psico-prova"
    assert read.modules == (EligibleModule(11, "Lezione 1"),)
    # the site key every chunk must carry: the normalised base_url of the session's site
    assert read.lms_site == "https://m.example"
    assert [o for o, _ in seen] == ["readable"] and seen[0][1] >= 0
    left = await pg.run(
        "super",
        "SELECT count(*) FROM assess_material_reads WHERE session_id = :s",
        {"s": sid},
    )
    assert left[0][0] == 0  # the API deleted the row it read


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (Source(Unreadable(UnreadableReason.TIMEOUT)), "modules_unreadable"),
        (Source(tag=None), "rag_off"),
    ],
)
async def test_an_unreadable_read_is_unreadable(
    pg: Pg, source: Source, reason: str
) -> None:
    _sid, read, seen = await _round_trip(pg, source)
    assert not read.readable and read.reason == reason and read.modules == ()
    assert [o for o, _ in seen] == ["unreadable"]


async def test_no_answer_in_time_is_a_timeout(pg: Pg) -> None:
    sid, site = await _session(pg)
    api = create_async_engine(pg.url("api"))
    seen: list[tuple[str, float]] = []
    try:
        reader = ApiEligibilityReader(
            api, timeout_s=0.5, observe=lambda o, s: seen.append((o, s))
        )
        read = await reader(sid, site)
    finally:
        await api.dispose()
    assert not read.readable and read.reason == "timeout"
    assert seen[0][0] == "timeout" and seen[0][1] >= 0.5


async def test_a_job_that_cannot_be_queued_is_enqueue_failed(pg: Pg) -> None:
    class Down:
        def begin(self) -> Any:
            raise ConnectionError("down")

    seen: list[tuple[str, float]] = []
    reader = ApiEligibilityReader(
        Down(), timeout_s=1, observe=lambda o, s: seen.append((o, s))
    )
    read = await reader(uuid.uuid4(), uuid.uuid4())
    assert not read.readable and read.reason == "enqueue_failed"
    assert [o for o, _ in seen] == ["enqueue_failed"]


@pytest.mark.parametrize("which", ["older_than_the_turn", "another_session"])
async def test_a_row_of_another_session_or_older_than_the_turn_is_never_used(
    pg: Pg, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    sid, site = await _session(pg)
    other, _other_site = await _session(pg)
    rid = uuid.uuid4()
    monkeypatch.setattr(material_reads, "uuid4", lambda: rid)
    row_sid, row_site = (sid, site) if which == "older_than_the_turn" else (other, site)
    await pg.run(
        "super",
        "INSERT INTO assess_material_reads (id, session_id, site_id, readable, namespace, "
        "modules, read_at) VALUES (:i, :s, :site, true, 'ns', CAST(:m AS jsonb), "
        + (
            "now() - interval '1 minute'"
            if which == "older_than_the_turn"
            else "clock_timestamp() + interval '1 second'"
        )
        + ")",
        {
            "i": rid,
            "s": row_sid,
            "site": row_site,
            "m": json.dumps([[11, "Lezione 1"]]),
        },
    )
    api = create_async_engine(pg.url("api"))
    try:
        read = await ApiEligibilityReader(api, timeout_s=0.6)(sid, site)
    finally:
        await api.dispose()
    assert not read.readable and read.reason == "timeout"


async def test_the_sweeper_deletes_late_rows_only(pg: Pg) -> None:
    sid, site = await _session(pg)
    old, fresh = uuid.uuid4(), uuid.uuid4()
    for rid, age in ((old, "10 minutes"), (fresh, "1 second")):
        await pg.run(
            "super",
            "INSERT INTO assess_material_reads (id, session_id, site_id, readable, reason, "
            f"read_at) VALUES (:i, :s, :site, false, 'timeout', now() - interval '{age}')",
            {"i": rid, "s": sid, "site": site},
        )
    worker = create_async_engine(pg.url("worker"))
    try:
        deleted = await sweep_material_reads(_acquire(worker))
    finally:
        await worker.dispose()
    assert deleted >= 1
    left = {
        r[0]
        for r in await pg.run(
            "super",
            "SELECT id FROM assess_material_reads WHERE session_id = :s",
            {"s": sid},
        )
    }
    assert left == {fresh}


async def test_the_api_reads_no_row_of_another_site(pg: Pg) -> None:
    """RLS (a0007): the API role bound to site A never sees site B's eligibility rows."""
    sid, site = await _session(pg)
    _other, other_site = await _session(pg)
    await pg.run(
        "super",
        "INSERT INTO assess_material_reads (id, session_id, site_id, readable, reason) "
        "VALUES (:i, :s, :site, false, 'timeout')",
        {"i": uuid.uuid4(), "s": sid, "site": site},
    )
    rows = await pg.run(
        "api", "SELECT count(*) FROM assess_material_reads", site=other_site
    )
    assert rows[0][0] == 0
    rows = await pg.run("api", "SELECT count(*) FROM assess_material_reads", site=site)
    assert rows[0][0] >= 1


async def test_the_row_shape_is_enforced(pg: Pg) -> None:
    from sqlalchemy.exc import IntegrityError

    sid, site = await _session(pg)
    with pytest.raises(IntegrityError, match="ck_assess_material_reads_shape"):
        await pg.run(
            "super",
            "INSERT INTO assess_material_reads (id, session_id, site_id, readable) "
            "VALUES (:i, :s, :site, true)",
            {"i": uuid.uuid4(), "s": sid, "site": site},
        )


async def test_a_site_without_a_usable_base_url_reads_nothing(pg: Pg) -> None:
    """No site key, no filter on it: the turn has no material and queues no job."""
    sid, site = await _session(pg)
    await pg.run(
        "super",
        "UPDATE assess_sites SET base_url = 'not a url' WHERE id = :s",
        {"s": site},
    )
    api = create_async_engine(pg.url("api"))
    try:
        read = await ApiEligibilityReader(api, timeout_s=1)(sid, site)
    finally:
        await api.dispose()
    assert not read.readable and read.reason == "site_unknown"
    jobs = await pg.run(
        "super",
        "SELECT count(*) FROM assess_jobs WHERE kind = 'material_eligibility' "
        "AND args->>'session_id' = :s",
        {"s": str(sid)},
    )
    assert jobs[0][0] == 0


async def test_a_teacher_withdrawal_stops_the_material_of_the_next_turn(pg: Pg) -> None:
    """FEAT-028 review 2: the teacher turns RAG OFF mid-session -> the next read gives no
    material (rag_withdrawn), whatever the session froze at Start."""
    sid, site = await _session(pg)
    await pg.run(
        "super",
        "UPDATE assess_activity_config SET rag_enabled = false, rag_attested_at = NULL, "
        "rag_attested_by = NULL WHERE site_id = :s",
        {"s": site},
    )
    source = Source()
    worker = create_async_engine(pg.url("worker"))
    try:
        out = await read_material(source, _acquire(worker), sid, uuid.uuid4())
    finally:
        await worker.dispose()
    assert out["ok"] is False and out["reason"] == "rag_withdrawn"
    assert source.calls == 0  # no LMS read at all

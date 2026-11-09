"""Capability probes: one per flag of ``vektra_assess.capabilities.FLAGS`` (ARCH-070).

A probe passes (``readable``) only if the reader's OUTPUT equals the value recorded in the
site's probe manifest. "The call returned 200" is not evidence (project rule: verify
empirically): a deliberately wrong expected value makes the probe ``unreadable``.

Probe manifest (``assess_sites.probe_manifest``, JSON; derived from the seed manifest by
``derive_probe_manifest``; the worker owns it, the API role cannot read it)::

    {"version": 1, "release": "...", "courseid": 3, "seeded_at": 1793619221,
     "flags": {"<flag>": {"checks": [
         {"args": {...},              # what the probe passes to the reader
          "kind": "value",            # value | partial | unreadable
          "expect": <json>,           # kind=value: the projection the reader must return
          "reason": "<UnreadableReason value>",   # kind=unreadable: why, by design
          "open_until": 1793615621}   # optional: from this time on, kind=partial
     ]}},
     "required_reads": {"quiz": {...}, "assign": {...}, ...}}   # ids for the setup check

Outcome of one check (kind decides what is expected; any other outcome is ``unreadable``,
fail closed):

- ``value``: the reader is Readable and its projection equals ``expect`` -> readable;
- ``partial``: the reader is ``Unreadable(partial)`` (caller-relative answer, e.g. the grace
  period of a closed quiz) -> partial;
- ``unreadable``: the reader is Unreadable (the reason actually seen is recorded) -> unreadable.

A flag's state is ``unreadable`` if any check is, else ``partial`` if any check is, else
``readable``. A flag with no checks in the manifest is ``unreadable/not_probed``.

``evidence_hash`` = sha256 over the canonical SHAPE signature (keys, types, list counts) of what
the probe saw, plus the release (``moodle/shape.py``): content-free, no value, no id.

The matrix rows of one check run are written by ``persist_matrix`` inside the caller's single
transaction (after ``capabilities.observe_release``): a crash leaves all of it or none of it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.capabilities import FLAGS
from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.readers import (
    read_assign_close_inputs,
    read_assign_release,
    read_assign_settings,
    read_attempt_review,
    read_enrolment,
    read_grade_release,
    read_graders,
    read_loginas_events,
    read_module_visibility_for_user,
    read_quiz_close_inputs,
    read_quiz_overrides,
    read_rubric_display_options,
    read_user_attempts,
    read_user_review_options,
)
from vektra_assess.moodle.result import Readable, Reading, Unreadable, UnreadableReason
from vektra_assess.moodle.shape import evidence_for

log = structlog.get_logger(__name__)

MANIFEST_VERSION = 1
#: the substring every hidden-feedback canary of the seed carries (``CANARY-GENFB-<q>-<tag>``)
HIDDEN_MARKER_PREFIX = "CANARY-GENFB"


# --- probe manifest ----------------------------------------------------------------------------


def _rows(rows: list[list[Any]]) -> list[list[Any]]:
    return sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))


def derive_probe_manifest(seed: Mapping[str, Any]) -> dict[str, Any]:
    """The probe manifest for one site, from the seed manifest (what the seeder put into
    Moodle, never what Moodle answered). Pure; raises ``KeyError`` on a malformed seed."""
    users: dict[str, int] = {k: int(v["id"]) for k, v in seed["users"].items()}
    groups: dict[str, int] = {k: int(v["id"]) for k, v in seed["groups"].items()}
    quizzes: Mapping[str, Any] = seed["quizzes"]
    assigns: Mapping[str, Any] = seed["assigns"]
    courseid = int(seed["courseid"])
    overrides_readable = bool(seed["ws_get_overrides_available"])

    def override_rows(name: str, field: str) -> list[list[Any]]:
        return _rows(
            [
                [
                    None if o["user"] is None else users[o["user"]],
                    None if o["group"] is None else groups[o["group"]],
                    o[field],
                ]
                for o in quizzes[name]["expected"]["overrides"]
            ]
        )

    def overrides_checks(field: str) -> list[dict[str, Any]]:
        if not overrides_readable:
            return [
                {
                    "args": {"quizid": quizzes["review_all"]["id"]},
                    "kind": "unreadable",
                    "reason": UnreadableReason.FUNCTION_NOT_IN_SERVICE.value,
                }
            ]
        return [
            {
                "args": {"quizid": quizzes[name]["id"]},
                "kind": "value",
                "expect": override_rows(name, field),
            }
            for name in ("review_all", "types")
        ]

    cutoff = assigns["cutoff"]
    by_user = cutoff["expected"]["reader_by_user"]
    flags: dict[str, Any] = {}
    flags["quiz_overrides"] = {"checks": overrides_checks("timeclose")}
    flags["per_student_timelimit"] = {"checks": overrides_checks("timelimit")}
    flags["assign_extensions"] = {
        "checks": [
            {
                "args": {
                    "assignid": cutoff["id"],
                    "users": [users["stu1"], users["stu2"]],
                },
                "kind": "value",
                "expect": _rows(
                    [
                        [users["stu1"], None],
                        [users["stu2"], by_user["stu2"]["extensionduedate"]],
                    ]
                ),
            }
        ]
    }
    flags["assign_user_overrides"] = {
        "checks": [
            {
                "args": {
                    "assignid": cutoff["id"],
                    "users": [users["stu1"], users["stu4"]],
                },
                "kind": "value",
                "expect": _rows(
                    [
                        [
                            users["stu1"],
                            cutoff["expected"]["duedate"],
                            cutoff["expected"]["cutoffdate"],
                        ],
                        [
                            users["stu4"],
                            by_user["stu4"]["duedate"],
                            by_user["stu4"]["cutoffdate"],
                        ],
                    ]
                ),
            }
        ]
    }
    flags["assign_cutoff"] = {
        "checks": [
            {
                "args": {"assigns": [assigns[n]["id"] for n in ("cutoff", "nocutoff")]},
                "kind": "value",
                "expect": _rows(
                    [
                        [
                            assigns[n]["id"],
                            assigns[n]["expected"]["duedate"],
                            assigns[n]["expected"]["cutoffdate"],
                        ]
                        for n in ("cutoff", "nocutoff")
                    ]
                ),
            }
        ]
    }
    flags["quiz_grace"] = {
        "checks": [
            {
                "args": {"quizid": quizzes[n]["id"]},
                "kind": "value",
                "expect": [
                    quizzes[n]["id"],
                    quizzes[n]["expected"]["graceperiod"],
                    quizzes[n]["expected"]["overduehandling"],
                ],
                # readable only while the quiz is open for the service account (spike)
                "open_until": quizzes[n]["expected"]["timeclose"],
            }
            for n in ("review_all", "inprogress")
        ]
    }

    def attempt_check(qname: str, user: str) -> dict[str, Any]:
        rows = [
            [
                a["id"],
                a["attempt_number"],
                a["state"],
                a["expected_sumgrades"] if a["state"] == "finished" else None,
            ]
            for a in quizzes[qname]["attempts"]
            if a["user"] == user
        ]
        return {
            "args": {"quizid": quizzes[qname]["id"], "userid": users[user]},
            "kind": "value",
            "expect": rows,
        }

    flags["attempt_state"] = {
        "checks": [
            attempt_check(q, u)
            for q, u in (
                ("gm_average", "stu1"),
                ("essay", "stu1"),
                ("essay", "stu2"),
                ("abandoned", "stu1"),
                ("inprogress", "stu1"),
            )
        ]
    }
    essay = {a["user"]: a for a in quizzes["essay"]["attempts"]}
    flags["grading_state"] = {
        "checks": [
            {
                "args": {"attempts": [essay["stu1"]["id"], essay["stu2"]["id"]]},
                "kind": "value",
                "expect": _rows(
                    [
                        [
                            a["id"],
                            "needsgrading"
                            in (a["expected_slot_states"] or {}).values(),
                            a["expected_sumgrades"],
                        ]
                        for a in essay.values()
                    ]
                ),
            }
        ]
    }

    def review_row(qname: str) -> list[Any]:
        rev = quizzes[qname]["expected"]["review_after_close"]
        return [
            quizzes[qname]["id"],
            rev["specificfeedback"],
            rev["generalfeedback"],
            rev["rightanswer"],
            rev["overallfeedback"],
            rev["marks"],
        ]

    review_names = ("review_all", "review_mixed", "review_none")
    flags["review_options_per_student"] = {
        "checks": [
            {
                "args": {
                    "quizzes": [
                        [
                            quizzes[n]["id"],
                            users[quizzes[n]["attempts"][0]["user"]],
                        ]
                        for n in review_names
                    ]
                },
                "kind": "value",
                "expect": _rows([review_row(n) for n in review_names]),
            }
        ]
    }
    marker = next(
        (
            "-".join(c.split("-")[:2])
            for c in seed.get("canaries", [])
            if c.startswith(HIDDEN_MARKER_PREFIX)
        ),
        None,
    )
    flags["review_html_filterable"] = {
        "checks": (
            []
            if marker is None
            else [
                {
                    "args": {
                        "attemptid": quizzes["review_none"]["attempts"][0]["id"],
                        "marker": marker,
                    },
                    # the review HTML of a student who may not see the general feedback still
                    # carries it: the HTML is NOT filterable (spike trap 4)
                    "kind": "unreadable",
                    "reason": UnreadableReason.PARTIAL.value,
                }
            ]
        )
    }
    flags["rubric_display_settings"] = {
        "checks": [
            {
                "args": {
                    "definitionid": assigns["rubric_vis"]["expected"]["rubric"][
                        "definitionid"
                    ]
                },
                "kind": "unreadable",
                "reason": UnreadableReason.PARTIAL.value,
            }
        ]
    }
    flags["module_visibility_per_student"] = {
        "checks": [
            {
                "args": {
                    "cmid": assigns["restricted"]["cmid"],
                    "userid": users["stu3"],
                },
                "kind": "unreadable",
                "reason": UnreadableReason.FUNCTION_NOT_IN_SERVICE.value,
            }
        ]
    }
    flags["loginas_crosscheck"] = {
        "checks": [
            {
                "args": {"userid": users["stu1"]},
                "kind": "unreadable",
                "reason": UnreadableReason.FUNCTION_NOT_IN_SERVICE.value,
            }
        ]
    }
    flags["graders_resolvable"] = {
        "checks": [
            {
                "args": {},
                "kind": "value",
                "expect": sorted(users[g] for g in seed["graders"]),
            }
        ]
    }
    students = sorted(k for k, v in seed["users"].items() if v["role"] == "student")
    flags["enrolment_readable"] = {
        "checks": [
            {
                "args": {
                    "users": [users[u] for u in students]
                    + [users[g] for g in seed["graders"]]
                },
                "kind": "value",
                "expect": _rows(
                    [[users[u], True, True] for u in students]
                    + [[users[g], True, False] for g in seed["graders"]]
                ),
            }
        ]
    }

    release_rows: list[list[Any]] = []
    assign_pairs: list[list[int]] = []
    for name in ("wf", "hiddengrade", "hiddenuntil", "simple", "rubric_vis", "guide"):
        a = assigns[name]
        workflow = bool(a["expected"]["markingworkflow"])
        hidden = bool(a["expected"]["gradeishidden"])
        graded = {g["user"]: g["gradingstatus"] for g in a["grades"]}
        for user in sorted(graded) or ["stu1"]:
            status = graded.get(user)
            released = (
                not hidden
                and status in ("graded", "released")
                and (not workflow or status == "released")
            )
            assign_pairs.append([a["id"], users[user]])
            release_rows.append(["assign", a["id"], users[user], released])
    quiz_pairs: list[list[int]] = []
    for name in ("review_all", "review_none"):
        q = quizzes[name]
        user = q["attempts"][0]["user"]
        quiz_pairs.append([q["cmid"], users[user]])
        release_rows.append(
            ["quiz_hidden", q["cmid"], users[user], q["expected"]["grade_item_hidden"]]
        )
    flags["release_state"] = {
        "checks": [
            {
                "args": {"assigns": assign_pairs, "quizzes": quiz_pairs},
                "kind": "value",
                "expect": _rows(release_rows),
            }
        ]
    }

    return {
        "version": MANIFEST_VERSION,
        "release": seed["release"],
        "courseid": courseid,
        "seeded_at": seed["seeded_at"],
        "flags": flags,
        "required_reads": {
            "quiz": {
                "quizid": quizzes["review_all"]["id"],
                "userid": users["stu1"],
                "attemptid": quizzes["review_all"]["attempts"][0]["id"],
            },
            "assign": {"assignid": cutoff["id"], "userid": users["stu1"]},
            "grade": {"cmid": assigns["simple"]["cmid"], "userid": users["stu1"]},
            "grading": {
                "cmid": assigns["rubric_vis"]["cmid"],
                "definitionid": assigns["rubric_vis"]["expected"]["rubric"][
                    "definitionid"
                ],
            },
            "enrol": {"userid": users["stu1"]},
            "files": {"assignid": assigns["files"]["id"], "userid": users["stu1"]},
        },
    }


# --- projections ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeContext:
    client: MoodleWsClient
    site: SiteRef  # carries the release (``with_release``)
    courseid: int


Probe = Callable[[ProbeContext, Mapping[str, Any]], Awaitable[Reading[Any]]]


def _first_unreadable(readings: list[Reading[Any]]) -> Unreadable | None:
    return next((r for r in readings if isinstance(r, Unreadable)), None)


def norm(value: Any) -> Any:
    """Canonical form for comparison: JSON types only, floats rounded, dict keys sorted."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, Mapping):
        return {
            str(k): norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (list, tuple)):
        return [norm(v) for v in value]
    return str(value)


async def _quiz_overrides(ctx: ProbeContext, a: Mapping[str, Any]) -> Reading[Any]:
    res = await read_quiz_overrides(ctx.client, ctx.site, a["quizid"])
    if isinstance(res, Unreadable):
        return res
    return Readable(_rows([[o.userid, o.groupid, o.timeclose] for o in res.value]))


async def _per_student_timelimit(
    ctx: ProbeContext, a: Mapping[str, Any]
) -> Reading[Any]:
    res = await read_quiz_overrides(ctx.client, ctx.site, a["quizid"])
    if isinstance(res, Unreadable):
        return res
    return Readable(_rows([[o.userid, o.groupid, o.timelimit_s] for o in res.value]))


async def _assign_extensions(ctx: ProbeContext, a: Mapping[str, Any]) -> Reading[Any]:
    rows: list[list[Any]] = []
    for userid in a["users"]:
        res = await read_assign_close_inputs(
            ctx.client, ctx.site, a["assignid"], userid
        )
        if isinstance(res, Unreadable):
            return res
        rows.append([userid, res.value.extensionduedate])
    return Readable(_rows(rows))


async def _assign_user_overrides(
    ctx: ProbeContext, a: Mapping[str, Any]
) -> Reading[Any]:
    rows: list[list[Any]] = []
    for userid in a["users"]:
        res = await read_assign_close_inputs(
            ctx.client, ctx.site, a["assignid"], userid
        )
        if isinstance(res, Unreadable):
            return res
        rows.append([userid, res.value.duedate, res.value.cutoffdate])
    return Readable(_rows(rows))


async def _assign_cutoff(ctx: ProbeContext, a: Mapping[str, Any]) -> Reading[Any]:
    rows: list[list[Any]] = []
    for assignid in a["assigns"]:
        res = await read_assign_settings(ctx.client, ctx.site, ctx.courseid, assignid)
        if isinstance(res, Unreadable):
            return res
        rows.append([assignid, res.value.duedate, res.value.cutoffdate])
    return Readable(_rows(rows))


async def _quiz_grace(ctx: ProbeContext, a: Mapping[str, Any]) -> Reading[Any]:
    res = await read_quiz_close_inputs(ctx.client, ctx.site, ctx.courseid, a["quizid"])
    if isinstance(res, Unreadable):
        return res
    grace = res.value.grace
    if isinstance(grace, Unreadable):
        return grace
    return Readable(
        [a["quizid"], grace.value.graceperiod_s, grace.value.overduehandling]
    )


async def _attempt_state(ctx: ProbeContext, a: Mapping[str, Any]) -> Reading[Any]:
    res = await read_user_attempts(ctx.client, ctx.site, a["quizid"], a["userid"])
    if isinstance(res, Unreadable):
        return res
    return Readable(
        [
            [
                x.attemptid,
                x.attempt_number,
                x.state.value,
                x.sumgrades if x.state.value == "finished" else None,
            ]
            for x in res.value
        ]
    )


async def _grading_state(ctx: ProbeContext, a: Mapping[str, Any]) -> Reading[Any]:
    rows: list[list[Any]] = []
    for attemptid in a["attempts"]:
        res = await read_attempt_review(ctx.client, ctx.site, attemptid)
        if isinstance(res, Unreadable):
            return res
        rows.append(
            [
                attemptid,
                any(s.needs_grading for s in res.value.slots),
                res.value.sumgrades,
            ]
        )
    return Readable(_rows(rows))


async def _review_options(ctx: ProbeContext, a: Mapping[str, Any]) -> Reading[Any]:
    rows: list[list[Any]] = []
    for quizid, userid in a["quizzes"]:
        res = await read_user_review_options(ctx.client, ctx.site, quizid, userid)
        if isinstance(res, Unreadable):
            return res
        v = res.value.all_attempts
        rows.append(
            [
                quizid,
                v.feedback,
                v.generalfeedback,
                v.rightanswer,
                v.overallfeedback,
                v.marks > 0,
            ]
        )
    return Readable(_rows(rows))


async def _review_html_filterable(
    ctx: ProbeContext, a: Mapping[str, Any]
) -> Reading[Any]:
    """Is the review HTML filtered per student? Looks for the hidden general-feedback marker
    in the HTML of a student whose options hide it: present -> NOT filterable (Unreadable)."""
    res = await read_attempt_review(ctx.client, ctx.site, a["attemptid"])
    if isinstance(res, Unreadable):
        return res
    marker = str(a["marker"])
    if any(s.html and marker in s.html for s in res.value.slots):
        return Unreadable(UnreadableReason.PARTIAL, "hidden_markup_in_html")
    return Readable([a["attemptid"], "marker_absent"])


async def _rubric_display(ctx: ProbeContext, a: Mapping[str, Any]) -> Reading[Any]:
    res = await read_rubric_display_options(ctx.client, ctx.site, a["definitionid"])
    return res if isinstance(res, Unreadable) else Readable(sorted(res.value))


async def _module_visibility(ctx: ProbeContext, a: Mapping[str, Any]) -> Reading[Any]:
    res = await read_module_visibility_for_user(
        ctx.site, ctx.courseid, a["cmid"], a["userid"]
    )
    return res if isinstance(res, Unreadable) else Readable([res.value])


async def _loginas(ctx: ProbeContext, a: Mapping[str, Any]) -> Reading[Any]:
    res = await read_loginas_events(ctx.site, ctx.courseid, a["userid"])
    return res if isinstance(res, Unreadable) else Readable([res.value])


async def _graders(ctx: ProbeContext, a: Mapping[str, Any]) -> Reading[Any]:
    res = await read_graders(ctx.client, ctx.site, ctx.courseid)
    if isinstance(res, Unreadable):
        return res
    return Readable(sorted(g.userid for g in res.value.graders))


async def _enrolment(ctx: ProbeContext, a: Mapping[str, Any]) -> Reading[Any]:
    rows: list[list[Any]] = []
    for userid in a["users"]:
        res = await read_enrolment(ctx.client, ctx.site, ctx.courseid, userid)
        if isinstance(res, Unreadable):
            return res
        rows.append([userid, res.value.active, res.value.is_student])
    return Readable(_rows(rows))


async def _release_state(ctx: ProbeContext, a: Mapping[str, Any]) -> Reading[Any]:
    rows: list[list[Any]] = []
    for assignid, userid in a["assigns"]:
        res = await read_assign_release(
            ctx.client, ctx.site, ctx.courseid, assignid, userid
        )
        if isinstance(res, Unreadable):
            return res
        rows.append(["assign", assignid, userid, res.value.released])
    for cmid, userid in a["quizzes"]:
        item = await read_grade_release(
            ctx.client, ctx.site, ctx.courseid, userid, cmid
        )
        if isinstance(item, Unreadable):
            return item
        rows.append(["quiz_hidden", cmid, userid, item.value.hidden])
    return Readable(_rows(rows))


#: one probe per flag; a test asserts the keys equal ``capabilities.FLAGS``.
PROBES: dict[str, Probe] = {
    "quiz_overrides": _quiz_overrides,
    "assign_extensions": _assign_extensions,
    "assign_cutoff": _assign_cutoff,
    "quiz_grace": _quiz_grace,
    "attempt_state": _attempt_state,
    "per_student_timelimit": _per_student_timelimit,
    "review_html_filterable": _review_html_filterable,
    "rubric_display_settings": _rubric_display,
    "module_visibility_per_student": _module_visibility,
    "loginas_crosscheck": _loginas,
    "graders_resolvable": _graders,
    "enrolment_readable": _enrolment,
    "release_state": _release_state,
    "grading_state": _grading_state,
    "assign_user_overrides": _assign_user_overrides,
    "review_options_per_student": _review_options,
}


# --- running -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FlagResult:
    flag: str
    state: str  # readable | partial | unreadable
    reason_code: str | None  # an UnreadableReason value; None when readable
    evidence_hash: str


@dataclass(frozen=True)
class _CheckOutcome:
    state: str
    reason: str | None
    shape: Any  # content-free: fed to the shape signature only


def _judge(check: Mapping[str, Any], reading: Reading[Any], now: int) -> _CheckOutcome:
    kind = str(check.get("kind", "value"))
    open_until = check.get("open_until")
    if isinstance(open_until, int) and now >= open_until:
        kind = "partial"
    if isinstance(reading, Unreadable):
        shape = {f"unreadable_{reading.reason.value}": 1}
        if kind == "partial" and reading.reason is UnreadableReason.PARTIAL:
            return _CheckOutcome("partial", UnreadableReason.PARTIAL.value, shape)
        return _CheckOutcome("unreadable", reading.reason.value, shape)
    shape = reading.value
    if kind == "value" and norm(reading.value) == norm(check.get("expect")):
        return _CheckOutcome("readable", None, shape)
    # readable where unreadable/partial was expected, or a value that is not the seeded one:
    # the reader cannot be trusted for this flag, so it is unreadable (fail closed)
    return _CheckOutcome("unreadable", UnreadableReason.UNKNOWN_SHAPE.value, shape)


def _aggregate(flag: str, release: str, outcomes: list[_CheckOutcome]) -> FlagResult:
    evidence = evidence_for(flag, release, [o.shape for o in outcomes]).signature
    bad = next((o for o in outcomes if o.state == "unreadable"), None)
    if bad is not None:
        return FlagResult(flag, "unreadable", bad.reason, evidence)
    if any(o.state == "partial" for o in outcomes):
        return FlagResult(flag, "partial", UnreadableReason.PARTIAL.value, evidence)
    return FlagResult(flag, "readable", None, evidence)


async def run_probes(
    client: MoodleWsClient,
    site: SiteRef,
    manifest: Mapping[str, Any] | None,
    *,
    now: int | None = None,
) -> tuple[FlagResult, ...]:
    """Probe every flag; the result always has one entry per flag, in ``FLAGS`` order.

    ``site.release`` must be the release ``site_info`` reported (``with_release``). A probe
    that raises is ``unreadable/unknown_shape`` (nothing is logged but the class name).
    """
    clock = int(time.time()) if now is None else now
    release = site.release or ""
    flags: Mapping[str, Any] = (manifest or {}).get("flags", {}) if manifest else {}
    ctx = ProbeContext(client, site, int((manifest or {}).get("courseid", 0)))
    out: list[FlagResult] = []
    for flag in FLAGS:
        checks = list((flags.get(flag) or {}).get("checks") or [])
        if not checks:
            out.append(
                FlagResult(
                    flag,
                    "unreadable",
                    UnreadableReason.NOT_PROBED.value,
                    evidence_for(flag, release, "not_probed").signature,
                )
            )
            continue
        outcomes: list[_CheckOutcome] = []
        for check in checks:
            try:
                reading = await PROBES[flag](ctx, check.get("args") or {})
            except Exception as exc:
                log.warning(
                    "capability_probe_error", flag=flag, error=type(exc).__name__
                )
                reading = Unreadable(UnreadableReason.UNKNOWN_SHAPE, "probe_error")
            outcomes.append(_judge(check, reading, clock))
        out.append(_aggregate(flag, release, outcomes))
    log.info(
        "capability_probes_done",
        **{
            s: sum(1 for r in out if r.state == s)
            for s in ("readable", "partial", "unreadable")
        },
    )
    return tuple(out)


async def persist_matrix(
    conn: AsyncConnection, site_id: Any, release: str, results: tuple[FlagResult, ...]
) -> None:
    """Write every flag row. The CALLER owns the transaction (the whole check run is one):
    all rows or none. ``release`` is the one ``observe_release`` recorded.

    ``assess_sites.release`` is "the release whose capability matrix is valid" (coordinator
    decision 2026-11-08): the render check and the pre-flight pick their reader model set from
    it. It moves to ``release`` here, in the same transaction, only when at least one flag was
    actually probed: a matrix of ``not_probed`` rows (no probe manifest) vouches for nothing.
    """
    probed = any(r.reason_code != UnreadableReason.NOT_PROBED.value for r in results)
    if probed:
        await conn.execute(
            text("UPDATE assess_sites SET release = :r WHERE id = :s"),
            {"r": release, "s": site_id},
        )
    for r in results:
        await conn.execute(
            text(
                "INSERT INTO assess_site_capabilities "
                "(site_id, flag, state, release, reason_code, evidence_hash, checked_at) "
                "VALUES (:s, :f, :st, :rel, :rc, :eh, now()) "
                "ON CONFLICT (site_id, flag) DO UPDATE SET state = EXCLUDED.state, "
                "release = EXCLUDED.release, reason_code = EXCLUDED.reason_code, "
                "evidence_hash = EXCLUDED.evidence_hash, checked_at = now()"
            ),
            {
                "s": site_id,
                "f": r.flag,
                "st": r.state,
                "rel": release,
                "rc": r.reason_code,
                "eh": r.evidence_hash,
            },
        )

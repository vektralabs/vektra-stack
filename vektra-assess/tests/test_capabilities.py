"""Fail-closed capability decisions (pure) and the import rules of the DB-only module."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vektra_assess.capabilities import (
    CLOSE_TIME_FLAGS,
    FLAGS,
    STALE_AFTER,
    CapabilityRow,
    NotReady,
    Ready,
    classify,
    evaluate_readiness,
)
from vektra_assess.readable import Readable, Unreadable, UnreadableReason

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
REL = "5.2.4+"


def row(
    flag="quiz_overrides",
    state="readable",
    release=REL,
    age=timedelta(hours=1),
    reason=None,
):
    return CapabilityRow(flag, state, release, reason, "h", NOW - age)


def test_flags_are_the_closed_list_and_close_time_flags_are_part_of_it():
    assert len(FLAGS) == len(set(FLAGS)) == 16
    assert set(CLOSE_TIME_FLAGS) <= set(FLAGS)
    assert STALE_AFTER == timedelta(hours=26)


def test_fresh_readable_row_on_the_current_release_is_readable():
    assert isinstance(classify(row(), REL, NOW), Readable)


@pytest.mark.parametrize(
    ("kw", "release", "reason", "detail"),
    [
        (
            {"age": timedelta(hours=26, seconds=1)},
            REL,
            UnreadableReason.NOT_PROBED,
            "stale",
        ),
        ({"release": "5.2.3"}, REL, UnreadableReason.RELEASE_CHANGED, ""),
        ({}, None, UnreadableReason.RELEASE_CHANGED, ""),
        ({"state": "partial"}, REL, UnreadableReason.PARTIAL, ""),
        (
            {"state": "unreadable", "reason": "access_exception"},
            REL,
            UnreadableReason.ACCESS_EXCEPTION,
            "",
        ),
        (
            {"state": "unreadable", "reason": "weird"},
            REL,
            UnreadableReason.NOT_PROBED,
            "",
        ),
        ({"state": "unreadable"}, REL, UnreadableReason.NOT_PROBED, ""),
    ],
)
def test_every_other_case_is_unreadable_with_the_specific_reason(
    kw, release, reason, detail
):
    r = classify(row(**kw), release, NOW)
    assert isinstance(r, Unreadable) and (r.reason, r.detail_code) == (reason, detail)


def test_missing_row_is_not_probed():
    assert classify(None, REL, NOW) == Unreadable(UnreadableReason.NOT_PROBED)


def test_exactly_26_hours_is_still_fresh():
    assert isinstance(classify(row(age=STALE_AFTER), REL, NOW), Readable)


def rows(**override):
    out = {f: row(f) for f in CLOSE_TIME_FLAGS}
    out.update(override)
    return out


OK_ITEMS = [
    {"name": "token_works", "result": "OK"},
    {"name": "not_site_admin", "result": "OK"},
]


def ready(**kw):
    base = dict(site_found=True, disabled=False, last_seen_release=REL, report_items=OK_ITEMS,
                rows=rows(), now=NOW)  # fmt: skip
    return evaluate_readiness(**{**base, **kw})


def test_ready_needs_everything():
    assert ready() == Ready()


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"site_found": False}, ("site_unavailable",)),
        ({"disabled": True}, ("site_unavailable",)),
        ({"last_seen_release": None}, ("never_checked",)),
        ({"report_items": None}, ("no_report_for_current_release",)),
        (
            {"report_items": [{"name": "token_works", "result": "MISSING"}]},
            ("token_works",),
        ),
        ({"report_items": [{"name": "x", "result": "UNVERIFIABLE"}]}, ("x",)),
        ({"rows": {}}, tuple(f"capability:{f}" for f in CLOSE_TIME_FLAGS)),
        (
            {"rows": rows(quiz_grace=row("quiz_grace", age=timedelta(hours=27)))},
            ("capability:quiz_grace",),
        ),
        (
            {"rows": rows(quiz_grace=row("quiz_grace", release="5.2.3"))},
            ("capability:quiz_grace",),
        ),
    ],
)
def test_not_ready_names_what_is_missing(kw, expected):
    assert ready(**kw) == NotReady(expected)


SRC = Path(__file__).resolve().parents[1] / "src" / "vektra_assess"


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_db_only_modules_do_not_import_httpx_or_the_moodle_package():
    """capabilities and the API never talk to Moodle (plan group D); import-linter contract (c)
    covers vektra_assess.moodle, this covers the external httpx package."""
    files = [
        SRC / "capabilities.py",
        SRC / "readable.py",
        *sorted((SRC / "api").rglob("*.py")),
    ]
    for f in files:
        bad = {
            m
            for m in _imports(f)
            if m.split(".")[0] == "httpx" or m.startswith("vektra_assess.moodle")
        }
        assert not bad, f"{f.name} imports {sorted(bad)}"


def test_a_failed_probe_on_a_close_time_flag_blocks_the_site_but_by_design_states_do_not():
    bad = rows(
        attempt_state=row("attempt_state", state="unreadable", reason="unknown_shape")
    )
    assert ready(rows=bad) == NotReady(("capability:attempt_state",))
    denied = rows(
        attempt_state=row(
            "attempt_state", state="unreadable", reason="access_exception"
        )
    )
    assert ready(rows=denied) == NotReady(("capability:attempt_state",))
    by_design = rows(
        quiz_overrides=row(
            "quiz_overrides", state="unreadable", reason="function_not_in_service"
        ),
        quiz_grace=row("quiz_grace", state="partial"),
    )
    assert ready(rows=by_design) == Ready()

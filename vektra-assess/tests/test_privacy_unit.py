"""Plan 12a unit tests: the REQ-089 settings rules, the summary, the setup-check item, the
erasure-mode startup rule, the purge notice text and the privacy routes' scopes."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from test_route_inventory import routes

from vektra_assess.config import VektraAssessSettings, erasure_mode_problem
from vektra_assess.delivery import notify
from vektra_assess.errors import (
    erasure_not_completed,
    privacy_config_invalid,
    transcript_expired,
)
from vektra_assess.jobs import JobKind
from vektra_assess.privacy import config as pc
from vektra_assess.privacy.subject import subject_ref
from vektra_assess.worker.setup_items import privacy_config_item
from vektra_shared.errors import http_status_for

GOOD = {
    "retention_days": 30,
    "legal_basis_label": "Art. 6(1)(e) GDPR",
    "legal_basis_text": "Valutazione dell'apprendimento.",
    "notice_url": "https://uni.example/privacy",
    "notice_text": "Informativa.",
    "notice_version": "v1",
}


def test_complete_settings_pass() -> None:
    assert pc.field_errors(GOOD) == []
    assert pc.passes(SimpleNamespace(**GOOD)) is True


@pytest.mark.parametrize(
    ("over", "field", "code"),
    [
        ({"retention_days": 0}, "retention_days", "out_of_range"),
        ({"retention_days": 3651}, "retention_days", "out_of_range"),
        ({"retention_days": True}, "retention_days", "not_an_integer"),
        ({"retention_days": "30"}, "retention_days", "not_an_integer"),
        ({"retention_days": None}, "retention_days", "required"),
        ({"legal_basis_label": "   "}, "legal_basis_label", "empty"),
        ({"legal_basis_label": "x" * 201}, "legal_basis_label", "too_long"),
        ({"notice_url": "http://uni.example/p"}, "notice_url", "not_https"),
        ({"notice_url": "https://"}, "notice_url", "too_short"),
        ({"notice_url": "https:///privacy"}, "notice_url", "invalid_url"),
        ({"notice_url": "https://[invalid"}, "notice_url", "invalid_url"),
        ({"notice_text": 5}, "notice_text", "not_a_string"),
        ({"notice_version": "v\x00"}, "notice_version", "control_character"),
    ],
)
def test_each_bound_is_enforced(over: dict[str, Any], field: str, code: str) -> None:
    errors = pc.field_errors({**GOOD, **over})
    assert [(e["field"], e["code"]) for e in errors] == [(field, code)]


def test_missing_and_unknown_fields_are_named_never_echoed() -> None:
    doc = {k: v for k, v in GOOD.items() if k != "notice_text"}
    doc["secret_field"] = "value-xyz"
    errors = pc.field_errors(doc)
    assert {(e["field"], e["code"]) for e in errors} == {
        ("notice_text", "required"),
        ("secret_field", "unknown_field"),
    }
    assert "value-xyz" not in repr(privacy_config_invalid(errors).to_envelope())


def test_a_row_out_of_bounds_is_not_configured() -> None:
    """The read side checks the bounds again: a row that does not pass is unset, never a
    default."""
    assert pc.passes(None) is False
    assert pc.passes(SimpleNamespace(**{**GOOD, "retention_days": 0})) is False

    class Conn:
        async def execute(self, *_a: Any) -> Any:
            return SimpleNamespace(
                first=lambda: SimpleNamespace(
                    **{**GOOD, "retention_days": 4000},
                    site_id=1,
                    updated_by="x",
                    updated_at=None,
                )
            )

    assert asyncio.run(pc.is_configured(Conn(), uuid.uuid4())) is False
    assert asyncio.run(pc.is_configured(None, uuid.uuid4())) is False


def test_the_summary_always_carries_the_mailbox_sentence() -> None:
    settings = pc.PrivacySettings(site_id=uuid.uuid4(), updated_by="t", **GOOD)
    block = pc.pre_session_privacy(settings)
    assert pc.MAILBOX_SENTENCE in block["summary"]
    assert "30 giorni" in block["summary"]
    assert block["mailbox_notice"] == pc.MAILBOX_SENTENCE
    assert block["notice_version"] == "v1"


def test_subject_ref_is_per_site_and_stable() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert subject_ref(a, 7) == subject_ref(a, 7)
    assert subject_ref(a, 7) != subject_ref(b, 7)
    assert subject_ref(a, 7) != subject_ref(a, 8)
    assert "7" != subject_ref(a, 7) and len(subject_ref(a, 7)) == 64


def test_new_error_codes_have_their_status() -> None:
    assert http_status_for(transcript_expired()) == 410
    pending = erasure_not_completed("pending")
    assert http_status_for(pending) == 503 and pending.retry_after == 5
    assert pending.details == {"reason": "pending"}
    assert http_status_for(privacy_config_invalid([])) == 400


@pytest.mark.parametrize(
    ("env", "mode", "enabled", "refused"),
    [
        ("production", None, True, True),
        ("staging", None, True, True),
        ("development", None, True, False),
        ("production", "erase", True, False),
        ("production", "retain_exam_evidence", True, False),
        ("production", None, False, False),
    ],
)
def test_the_erasure_mode_is_never_implicit_outside_development(
    env: str, mode: str | None, enabled: bool, refused: bool
) -> None:
    doc: dict[str, Any] = {"VEKTRA_ASSESS_ENABLED": enabled, "VEKTRA_ENV": env}
    if mode:
        doc["VEKTRA_ASSESS_ERASURE_MODE"] = mode
    settings = VektraAssessSettings.model_validate(doc)
    assert (erasure_mode_problem(settings) is not None) is refused


def test_privacy_config_item_judges_the_stored_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def acquire() -> Any:
        yield object()

    results = iter([False, True])

    async def configured(_conn: Any, _site: Any) -> bool:
        return next(results)

    monkeypatch.setattr(pc, "is_configured", configured)
    check = privacy_config_item(acquire)
    ctx = SimpleNamespace(site=SimpleNamespace(site_id=str(uuid.uuid4())))
    first = asyncio.run(check(ctx))  # type: ignore[arg-type]
    second = asyncio.run(check(ctx))  # type: ignore[arg-type]
    assert (first.result, first.reason_code) == ("MISSING", "privacy_config_unset")
    assert second.result == "OK"


def test_the_purge_notice_is_content_free() -> None:
    site = uuid.uuid4()
    args = {
        "site_id": str(site),
        "cmid": 17,
        "student_userid": 5,
        "session_no": 2,
        "reason": notify.PURGE_ABANDONED,
    }
    event = asyncio.run(notify.read_event(None, JobKind.TEACHER_NOTIFY, args))
    assert event is not None and event.code == notify.PURGE_ABANDONED
    subject, body = notify._text(JobKind.TEACHER_NOTIFY, event)
    assert "attivita 17" in subject and "studente 5" in subject and "#2" in subject
    assert "conservazione" in body
    # never for the operator, never without its ids
    assert asyncio.run(notify.read_event(None, JobKind.OPERATOR_NOTIFY, args)) is None
    assert (
        asyncio.run(
            notify.read_event(None, JobKind.TEACHER_NOTIFY, {**args, "session_no": "x"})
        )
        is None
    )


PRIVACY_ROUTES = {
    ("/api/v1/assess/sites/{site_id}/privacy", "PUT"): "require_exact_scope_admin",
    ("/api/v1/assess/sites/{site_id}/privacy", "GET"): "require_exact_scope_admin",
    ("/api/v1/assess/privacy/subjects/{userid}", "DELETE"): (
        "require_exact_scope_assess:privacy-erase"
    ),
    ("/api/v1/assess/privacy/activities/{cmid}/subjects", "DELETE"): (
        "require_exact_scope_assess:privacy-erase"
    ),
    ("/api/v1/assess/privacy/subjects/{userid}/transcripts", "GET"): (
        "require_exact_scope_assess:privacy-export"
    ),
    ("/api/v1/assess/privacy/subjects/{userid}/activities", "GET"): (
        "require_exact_scope_assess:privacy-export"
    ),
    ("/api/v1/assess/privacy/activities/{cmid}/subjects", "GET"): (
        "require_exact_scope_assess:privacy-export"
    ),
}


def _names(dependant: Any) -> list[str]:
    out: list[str] = []
    for dep in dependant.dependencies:
        if dep.call is not None:
            out.append(getattr(dep.call, "__name__", repr(dep.call)))
        out.extend(_names(dep))
    return out


def test_every_privacy_route_requires_exactly_its_scope() -> None:
    found = {}
    for path, methods, dependant in routes():
        if "/privacy" not in path:
            continue
        for method in methods:
            found[(path, method)] = [
                n for n in _names(dependant) if n.startswith("require_")
            ]
    assert set(found) == set(PRIVACY_ROUTES)
    for key, scope in PRIVACY_ROUTES.items():
        assert found[key] == [scope], key


def test_every_subject_table_declares_its_export() -> None:
    """Findings 2+3 (#215): a table the erasure deletes cannot ship without saying how the
    subject export shows it (detail columns, or a count)."""
    from vektra_assess.privacy import subject

    assert set(subject.SUBJECT_EXPORT) == set(subject.SUBJECT_TABLES)
    for table, how in subject.SUBJECT_EXPORT.items():
        assert isinstance(how, subject.Detail | subject.Count), table
        q = subject.export_query(table)
        assert q.startswith("SELECT cmid, ") and f"FROM {table} " in q
        assert "student_userid = :u" in q and "cmid = ANY(:c)" in q
    # no free text: the reset reason and the grader's identity stay out (OQ-020)
    exported = {
        col
        for how in subject.SUBJECT_EXPORT.values()
        if isinstance(how, subject.Detail)
        for col, _ in how.columns
    }
    assert not exported & {"reason_ct", "reason_kid", "granted_by"}
    assert not set(subject.EXPORT_EXCLUDED) & set(subject.SUBJECT_TABLES)


def test_activity_erasure_body_is_strict() -> None:
    from fastapi import HTTPException

    from vektra_assess.api import privacy as api

    assert api._userids({}) is None
    assert api._userids({"userids": [5, 3, 5]}) == [3, 5]
    for bad in (
        {"userids": None},
        {"userids": []},
        {"userids": "5"},
        {"userids": [True]},
        {"userids": [0]},
        {"userids": ["5"]},
        {"userids": list(range(1, api.MAX_USERIDS + 2))},
        {"userid": [5]},
    ):
        with pytest.raises(HTTPException) as exc:
            api._userids(bad)
        assert exc.value.status_code == 400, bad


def test_the_settings_view_names_its_state_never_more() -> None:
    from vektra_assess.api import privacy as api

    assert api._view(None) == {"configured": False}
    row = SimpleNamespace(**GOOD, updated_by="api_key:x", updated_at=None)
    view = api._view(row)
    assert view["configured"] is True
    assert view["mailbox_notice"] == pc.MAILBOX_SENTENCE
    assert {k for k in view} == {*pc.FIELDS, "updated_by", "updated_at", "configured",
                                 "mailbox_notice"}  # fmt: skip
    out = api._view(SimpleNamespace(**{**GOOD, "retention_days": 0}, updated_by="x",
                                    updated_at=None))  # fmt: skip
    assert out["configured"] is False


def test_a_busy_pool_answers_pending_on_time() -> None:
    """CodeRabbit 4516399711: the erasure deadline covers the pool wait, not only the sleeps."""
    import time

    from fastapi import HTTPException

    from vektra_assess.api import privacy as api

    class Hanging:
        @asynccontextmanager
        async def connect(self) -> Any:
            await asyncio.sleep(30)  # an exhausted pool
            yield None

    settings = VektraAssessSettings.model_validate(
        {"VEKTRA_ASSESS_ERASURE_WAIT_S": 0.5}
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(assess_settings=settings))
    )
    started = time.monotonic()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api._wait(request, Hanging(), 1))  # type: ignore[arg-type]
    assert time.monotonic() - started < 2
    assert exc.value.status_code == 503
    assert exc.value.detail["error"]["details"] == {"reason": "pending"}


def test_the_overdue_threshold_is_never_below_the_in_flight_wait() -> None:
    """Review finding 7: an erasure legitimately waits up to ``IN_FLIGHT`` for a send that may
    be on the wire; alerting before that would report a request working as designed."""
    from pydantic import ValidationError

    from vektra_assess.privacy.purge import IN_FLIGHT

    low = int(IN_FLIGHT.total_seconds())
    field = VektraAssessSettings.model_fields["erasure_alert_after_s"]
    assert low in [getattr(m, "ge", None) for m in field.metadata]
    with pytest.raises(ValidationError):
        VektraAssessSettings.model_validate(
            {"VEKTRA_ASSESS_ERASURE_ALERT_AFTER_S": low - 1}
        )
    ok = VektraAssessSettings.model_validate(
        {"VEKTRA_ASSESS_ERASURE_ALERT_AFTER_S": low}
    )
    assert ok.erasure_alert_after_s == low


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("v1", None),
        ("x" * 40, None),
        ("x" * 41, "too_long"),
        ("", "empty"),
        ("  ", "empty"),
        ("v\x01", "control_character"),
        ("v\r", "control_character"),
        ("v\x7f", "control_character"),
        ("v\x80", "control_character"),
        ("v\x9f", "control_character"),
        ("v\u00a0x", None),  # NBSP is not a control character
        ("v\tx", None),
        (7, "not_a_string"),
        (None, "required"),
    ],
)
def test_one_text_field_has_the_same_rule_as_the_whole_row(value, code) -> None:  # type: ignore[no-untyped-def]
    """Plugin request P3: the echoed notice version, the token claim, the deployment stand-in
    and the stored row share one rule (``privacy_text``), never a copy of it."""
    from vektra_assess.privacy_text import notice_version_error, text_field_error

    one = text_field_error("notice_version", value)
    assert (one or {}).get("code") == code
    assert notice_version_error(value) == code
    row = [
        e
        for e in pc.field_errors({**GOOD, "notice_version": value})
        if e["field"] == "notice_version"
    ]
    assert row == ([one] if one else [])


def test_the_text_rule_module_has_no_database_dependency() -> None:
    """Review finding 8: ``tokens`` and the settings import the rule without the DB layer."""
    import ast
    from pathlib import Path

    import vektra_assess.privacy_text as mod

    tree = ast.parse(Path(mod.__file__).read_text())
    imported = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])  # type: ignore[attr-defined]
    }
    assert imported <= {"__future__", "typing", "urllib.parse"}, imported


def test_a_stored_row_with_del_or_c1_stays_readable() -> None:
    """Review finding 9: DEL and C1 are refused on every new write, but a row stored under
    the earlier rule stays configured (readable and startable)."""
    legacy = {**GOOD, "notice_text": "Informativa\x85."}
    assert pc.field_errors(legacy) == [
        {"field": "notice_text", "code": "control_character"}
    ]
    assert pc.passes(SimpleNamespace(**legacy)) is True
    assert pc.passes(SimpleNamespace(**{**GOOD, "notice_text": "x\x01"})) is False


@pytest.mark.parametrize("bad", ["", "   ", "v\r", "v\x85", "x" * 41])
def test_the_stand_in_version_meets_the_echo_rule(bad: str) -> None:
    """Review finding 3: /state can never show a stand-in the mint would refuse."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        VektraAssessSettings.model_validate(
            {"VEKTRA_ASSESS_PRIVACY_NOTICE_VERSION": bad}
        )
    assert "VEKTRA_ASSESS_PRIVACY_NOTICE_VERSION" in str(exc.value)
    assert (
        VektraAssessSettings.model_validate(
            {"VEKTRA_ASSESS_PRIVACY_NOTICE_VERSION": "2026-11"}
        ).privacy_notice_version
        == "2026-11"
    )


def test_the_mint_body_types_the_version_loosely() -> None:
    """Review finding 4: the model accepts any value; the route checks the format after the
    login-as assertion (``notice_version_problem``)."""
    from vektra_assess.api.models import MintRequest

    base = {
        "sub": 1,
        "cmid": 2,
        "loginas": False,
        "realuser_id": None,
        "plugin_version": "1",
    }
    assert MintRequest.model_validate(base).notice_version_problem() is None
    nulled = MintRequest.model_validate({**base, "privacy_notice_version": None})
    assert nulled.notice_version_problem() is None
    shown = MintRequest.model_validate({**base, "privacy_notice_version": "v1"})
    assert shown.privacy_notice_version == "v1"
    assert shown.notice_version_problem() is None
    bad = MintRequest.model_validate({**base, "privacy_notice_version": "x" * 41})
    assert bad.notice_version_problem() == "too_long:privacy_notice_version"
    bad = MintRequest.model_validate({**base, "privacy_notice_version": 7})
    assert bad.notice_version_problem() == "not_a_string:privacy_notice_version"


def test_a_shown_field_change_needs_a_new_version() -> None:
    """Review finding 1: the operator rule, on the pure function."""
    old = SimpleNamespace(**GOOD)
    assert pc.version_must_change(old, dict(GOOD)) is False
    for field in pc.SHOWN_FIELDS:
        new = {
            **GOOD,
            field: 31 if field == "retention_days" else "https://x.example/v2",
        }
        assert pc.version_must_change(old, new) is True, field
        assert pc.version_must_change(old, {**new, "notice_version": "v2"}) is False
    assert set(pc.SHOWN_FIELDS) == set(pc.FIELDS) - {"notice_version"}


def test_the_notice_changed_error_is_a_409() -> None:
    from vektra_assess.errors import privacy_notice_changed

    e = privacy_notice_changed()
    assert e.code == "ERR-ASSESS-029" and http_status_for(e) == 409
    assert e.details == {"reason": "privacy_notice_changed"}


def test_the_rows_left_are_logged_from_known_table_names_only() -> None:
    """CodeRabbit 4558071155: the alert's ``tables_left`` is built from the erasure's own table
    list (anything else counts as ``other``), never from exception text, and stays under the
    log value cap."""
    from vektra_assess.privacy.purge import LEFT_TABLES, tables_left

    assert tables_left({}) == "none"
    assert (
        tables_left({"assess_turns": 2, "assess_snapshots": 1}) == "snapshots=1,turns=2"
    )
    hostile = {"assess_turns": 1, "x'; DROP TABLE t; --": 3, "student text": 4}
    assert tables_left(hostile) == "other=7,turns=1"
    every = tables_left(dict.fromkeys(LEFT_TABLES, 123_456))
    assert len(every) <= 200 and every.endswith(",...")


def test_the_reset_grant_reason_leftover_is_named_in_the_alert() -> None:
    """CodeRabbit 4560721381: ``leftover`` records the unshredded reset-grant reasons under
    ``assess_reset_grants.reason``; the alert must name it, not fold it into ``other``."""
    from vektra_assess.privacy.purge import LEFT_TABLES, tables_left

    assert "assess_reset_grants.reason" in LEFT_TABLES
    left = {"assess_reset_grants.reason": 2, "assess_turns": 1}
    assert tables_left(left) == "reset_grants.reason=2,turns=1"


def test_the_alert_keys_survive_the_log_allowlist() -> None:
    """CodeRabbit 4558071155: the operator reads ``tables_left``, ``erasure_id`` and
    ``erasure_scope`` from the worker's log, so the allowlist processor must keep them."""
    from vektra_assess.privacy.purge import tables_left
    from vektra_shared.log_safety import DROPPED, scrub_event_dict

    event = scrub_event_dict(
        {
            "event": "assess.erasure.alert",
            "alert": "erasure_verification_failed",
            "erasure_id": 41,
            "erasure_scope": "subject",
            "tables_left": tables_left({"assess_turns": 2}),
            "left": "anything",
        }
    )
    assert event["tables_left"] == "turns=2"
    assert event["erasure_id"] == 41 and event["erasure_scope"] == "subject"
    assert event["left"] == DROPPED

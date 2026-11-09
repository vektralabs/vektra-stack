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

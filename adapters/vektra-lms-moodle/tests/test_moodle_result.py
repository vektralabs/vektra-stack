from __future__ import annotations

import pytest

from vektra_lms_moodle.errors import (
    error_code,
    is_error_envelope,
    unreadable_from_envelope,
)
from vektra_lms_moodle.result import (
    Readable,
    Reading,
    Unreadable,
    UnreadableReason,
)


def describe(r: Reading[int]) -> str:
    match r:
        case Readable(value=v):
            return f"ok:{v}"
        case Unreadable(reason=reason):
            return f"no:{reason}"


def test_reading_is_matchable_and_the_twelve_reasons_are_the_arch_070_set():
    assert describe(Readable(3)) == "ok:3"
    assert describe(Unreadable(UnreadableReason.PARTIAL)) == "no:partial"
    assert {r.value for r in UnreadableReason} == {
        "function_not_in_service", "access_exception", "unknown_shape", "partial",
        "breaker_open", "timeout", "size_cap", "tls", "redirect", "version_unsupported",
        "not_probed", "release_changed",
    }  # fmt: skip


@pytest.mark.parametrize("code", ["has space", "x" * 65, "a;b"])
def test_detail_code_cannot_carry_free_text(code):
    with pytest.raises(ValueError):
        Unreadable(UnreadableReason.TIMEOUT, code)


def test_envelope_is_detected_and_only_the_errorcode_survives():
    doc = {
        "exception": "webservice_access_exception",
        "errorcode": "accessexception",
        "message": "Access control exception for Mario Rossi",
        "debuginfo": "secret",
    }
    assert is_error_envelope(doc)
    u = unreadable_from_envelope(doc)
    assert (u.reason, u.detail_code) == (
        UnreadableReason.ACCESS_EXCEPTION,
        "accessexception",
    )
    assert "Mario" not in repr(u) and "secret" not in repr(u)


def test_ordinary_payloads_are_not_envelopes():
    assert not is_error_envelope({"errorcode": "x"})  # a legitimate field is not enough
    assert not is_error_envelope([{"exception": 1, "errorcode": 2}])
    assert not is_error_envelope({"courses": []})


def test_unknown_or_hostile_errorcodes_fail_closed_and_are_sanitised():
    assert (
        unreadable_from_envelope({"exception": "e", "errorcode": "somethingnew"}).reason
        is UnreadableReason.UNKNOWN_SHAPE
    )
    assert (
        error_code({"exception": "e", "errorcode": "Mario Rossi <script>"})
        == "malformed"
    )
    assert error_code({"exception": "e", "errorcode": 7}) == "malformed"
    assert (
        unreadable_from_envelope({"exception": "e", "errorcode": "invalidtoken"}).reason
        is UnreadableReason.ACCESS_EXCEPTION
    )


def test_the_provider_re_export_is_the_same_object() -> None:
    """The readers' ``result`` module re-exports the shared outcome types: one class, not two,
    or an isinstance check across the boundary would silently fail (moved from the vertical's
    ``test_fake_lms.py``)."""
    from vektra_lms_moodle import result
    from vektra_shared import lms

    assert result.Readable is lms.Readable and result.Unreadable is lms.Unreadable
    assert result.UnreadableReason is lms.UnreadableReason

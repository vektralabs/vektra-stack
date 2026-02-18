"""Tests for error types and envelope serialization (REQ-010, REQ-011)."""
import pytest
from uuid import UUID, uuid4

from vektra_shared.errors import (
    ERR_AUTH_001,
    ERR_AUTH_002,
    ERR_AUTH_003,
    ERR_CONFIG_001,
    ERR_CONFIG_002,
    ERR_INGEST_001,
    ERR_INGEST_002,
    ERR_INGEST_003,
    ERR_INGEST_004,
    ERR_QUERY_001,
    ERR_QUERY_002,
    ERR_QUERY_003,
    ERR_QUERY_004,
    ErrorCategory,
    ErrorResponse,
    auth_insufficient_scope,
    auth_invalid_token,
    http_status_for,
)


class TestErrorResponseEnvelope:
    """REQ-010: ErrorResponse serializes to {"error": <fields>} envelope."""

    def test_to_envelope_shape(self):
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_AUTH_001,
            message="Token is invalid.",
            remediation="Provide a valid Bearer token.",
        )
        envelope = err.to_envelope()
        assert "error" in envelope
        body = envelope["error"]
        assert body["category"] == "PERMANENT"
        assert body["code"] == "ERR-AUTH-001"
        assert body["message"] == "Token is invalid."
        assert body["remediation"] == "Provide a valid Bearer token."
        assert "request_id" in body
        # UUID should be parseable
        UUID(body["request_id"])

    def test_retry_after_omitted_when_none(self):
        err = ErrorResponse(
            category=ErrorCategory.TRANSIENT,
            code="ERR-QUERY-004",
            message="Vector store unavailable.",
            remediation="Retry in a few seconds.",
        )
        envelope = err.to_envelope()
        assert "retry_after" not in envelope["error"]

    def test_retry_after_included_when_set(self):
        err = ErrorResponse(
            category=ErrorCategory.TRANSIENT,
            code="ERR-QUERY-004",
            message="Vector store unavailable.",
            remediation="Retry in a few seconds.",
            retry_after=5,
        )
        assert err.to_envelope()["error"]["retry_after"] == 5

    def test_request_id_is_stable_when_provided(self):
        fixed_id = uuid4()
        err = ErrorResponse(
            category=ErrorCategory.UPSTREAM,
            code=ERR_INGEST_004,
            message="Write failed.",
            remediation="Check vector store connectivity.",
            request_id=fixed_id,
        )
        assert err.to_envelope()["error"]["request_id"] == str(fixed_id)

    def test_details_empty_by_default(self):
        err = ErrorResponse(
            category=ErrorCategory.CONFIGURATION,
            code=ERR_CONFIG_001,
            message="Missing VEKTRA_LLM_PROVIDER.",
            remediation="Set VEKTRA_LLM_PROVIDER in your environment.",
        )
        assert err.to_envelope()["error"]["details"] == {}


class TestAllNormativeErrorCodes:
    """REQ-011: All 13 normative error codes exist as constants."""

    def test_ingest_codes_exist(self):
        for code in [ERR_INGEST_001, ERR_INGEST_002, ERR_INGEST_003, ERR_INGEST_004]:
            assert code.startswith("ERR-INGEST-")

    def test_query_codes_exist(self):
        for code in [ERR_QUERY_001, ERR_QUERY_002, ERR_QUERY_003, ERR_QUERY_004]:
            assert code.startswith("ERR-QUERY-")

    def test_config_codes_exist(self):
        for code in [ERR_CONFIG_001, ERR_CONFIG_002]:
            assert code.startswith("ERR-CONFIG-")

    def test_auth_codes_exist(self):
        for code in [ERR_AUTH_001, ERR_AUTH_002, ERR_AUTH_003]:
            assert code.startswith("ERR-AUTH-")

    def test_total_normative_count(self):
        all_codes = [
            ERR_INGEST_001, ERR_INGEST_002, ERR_INGEST_003, ERR_INGEST_004,
            ERR_QUERY_001, ERR_QUERY_002, ERR_QUERY_003, ERR_QUERY_004,
            ERR_CONFIG_001, ERR_CONFIG_002,
            ERR_AUTH_001, ERR_AUTH_002, ERR_AUTH_003,
        ]
        assert len(all_codes) == 13


class TestHttpStatusMapping:
    def test_transient_maps_to_503(self):
        err = ErrorResponse(
            category=ErrorCategory.TRANSIENT,
            code=ERR_QUERY_004,
            message="x",
            remediation="y",
        )
        assert http_status_for(err) == 503

    def test_permanent_maps_to_422(self):
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_INGEST_001,
            message="x",
            remediation="y",
        )
        assert http_status_for(err) == 422

    def test_configuration_maps_to_500(self):
        err = ErrorResponse(
            category=ErrorCategory.CONFIGURATION,
            code=ERR_CONFIG_001,
            message="x",
            remediation="y",
        )
        assert http_status_for(err) == 500

    def test_upstream_maps_to_502(self):
        err = ErrorResponse(
            category=ErrorCategory.UPSTREAM,
            code=ERR_INGEST_004,
            message="x",
            remediation="y",
        )
        assert http_status_for(err) == 502

    def test_auth_001_maps_to_401(self):
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_AUTH_001,
            message="x",
            remediation="y",
        )
        assert http_status_for(err) == 401

    def test_auth_003_maps_to_403(self):
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_AUTH_003,
            message="x",
            remediation="y",
        )
        assert http_status_for(err) == 403

    def test_ingest_002_maps_to_413(self):
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_INGEST_002,
            message="x",
            remediation="y",
        )
        assert http_status_for(err) == 413


class TestErrorFactories:
    def test_auth_invalid_token_has_remediation(self):
        err = auth_invalid_token()
        assert err.code == ERR_AUTH_001
        assert err.remediation != ""

    def test_auth_insufficient_scope_mentions_scope(self):
        err = auth_insufficient_scope("admin")
        assert err.code == ERR_AUTH_003
        assert "admin" in err.message
        assert err.remediation != ""

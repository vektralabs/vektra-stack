"""Unit tests for vektra_ingest.exceptions."""

from __future__ import annotations

from vektra_ingest.exceptions import IngestConflictError, IngestError


class TestIngestError:
    def test_fields_stored(self) -> None:
        err = IngestError(error_code="ERR-INGEST-003", message="Scanned PDF")
        assert err.error_code == "ERR-INGEST-003"
        assert err.message == "Scanned PDF"

    def test_str_is_message(self) -> None:
        err = IngestError(error_code="ERR-INGEST-001", message="Duplicate")
        assert str(err) == "Duplicate"


class TestIngestConflictError:
    def test_fields_stored(self) -> None:
        err = IngestConflictError(filename="doc.pdf", namespace="default")
        assert err.filename == "doc.pdf"
        assert err.namespace == "default"

    def test_str_includes_filename_and_namespace(self) -> None:
        err = IngestConflictError(filename="doc.pdf", namespace="ns1")
        msg = str(err)
        assert "doc.pdf" in msg
        assert "ns1" in msg

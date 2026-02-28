"""Exceptions for vektra-ingest pipeline errors."""

from __future__ import annotations


class IngestError(Exception):
    """Raised by the ingestion pipeline on unrecoverable extraction failures.

    error_code maps to ERR_INGEST_* constants in vektra_shared.errors.
    """

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        self.message = message
        super().__init__(message)


class IngestConflictError(Exception):
    """Raised when a different document with the same filename already exists (REQ-033).

    Caller should return HTTP 409.
    """

    def __init__(self, filename: str, namespace: str) -> None:
        self.filename = filename
        self.namespace = namespace
        super().__init__(
            f"Document '{filename}' already exists in namespace '{namespace}' "
            "with different content. Delete the existing document first."
        )

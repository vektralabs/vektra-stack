"""Unit tests for content type detection (ARCH-042, ARCH-011, REQ-058)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_detect_fallback_on_import_error():
    """ImportError (libmagic not installed) with unknown extension falls back to octet-stream."""
    from vektra_ingest.detection import detect_content_type

    with patch.dict("sys.modules", {"magic": None}):
        result = detect_content_type(b"some bytes", "file.bin")
    assert result == "application/octet-stream"


def test_detect_extension_fallback_when_magic_unavailable():
    """When libmagic is unavailable, known extensions return correct MIME type."""
    from vektra_ingest.detection import detect_content_type

    with patch.dict("sys.modules", {"magic": None}):
        assert detect_content_type(b"some bytes", "doc.pdf") == "application/pdf"
        assert detect_content_type(b"some bytes", "doc.docx") == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert detect_content_type(b"some bytes", "pres.pptx") == (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        )


def test_detect_returns_mime_from_magic():
    """Successful magic detection returns the detected MIME type."""
    from vektra_ingest.detection import detect_content_type

    mock_magic = MagicMock()
    mock_magic.from_buffer.return_value = "application/pdf"

    with patch.dict("sys.modules", {"magic": mock_magic}):
        result = detect_content_type(b"%PDF-1.4 fake content", "file.pdf")

    assert result == "application/pdf"
    mock_magic.from_buffer.assert_called_once()


def test_detect_fallback_on_empty_result():
    """Empty result from magic falls back to application/octet-stream."""
    from vektra_ingest.detection import detect_content_type

    mock_magic = MagicMock()
    mock_magic.from_buffer.return_value = ""

    with patch.dict("sys.modules", {"magic": mock_magic}):
        result = detect_content_type(b"data", "file.bin")

    assert result == "application/octet-stream"


def test_detect_fallback_on_exception():
    """Exception from magic.from_buffer falls back to application/octet-stream."""
    from vektra_ingest.detection import detect_content_type

    mock_magic = MagicMock()
    mock_magic.from_buffer.side_effect = RuntimeError("magic failed")

    with patch.dict("sys.modules", {"magic": mock_magic}):
        result = detect_content_type(b"data", "file.bin")

    assert result == "application/octet-stream"


def test_detect_uses_first_8kb():
    """Only first 8KB of content is sampled for detection."""
    from vektra_ingest.detection import detect_content_type

    mock_magic = MagicMock()
    mock_magic.from_buffer.return_value = "application/pdf"

    large_content = b"x" * 100_000

    with patch.dict("sys.modules", {"magic": mock_magic}):
        detect_content_type(large_content, "big.pdf")

    # Should have been called with only the first 8192 bytes
    called_with = mock_magic.from_buffer.call_args[0][0]
    assert len(called_with) == 8192

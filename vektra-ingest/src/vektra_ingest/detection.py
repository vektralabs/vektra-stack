"""Content type detection via magic bytes (ARCH-011, ARCH-042, REQ-058).

Uses python-magic (libmagic binding) to detect MIME type from file bytes.
Falls back to extension-based detection when libmagic is unavailable, then
to 'application/octet-stream' (BLOCKER B-2: content_type is never NULL).
"""

from __future__ import annotations

import os

import structlog

log = structlog.get_logger(__name__)

_FALLBACK = "application/octet-stream"

# Extension-based fallback map (used when libmagic is not available)
_EXT_MAP: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".ppt": "application/vnd.ms-powerpoint",
}


def detect_content_type(content: bytes, filename: str = "") -> str:
    """Detect MIME type from file bytes.

    Primary: python-magic (libmagic binding) - accurate magic byte detection.
    Fallback: file extension mapping when libmagic is unavailable.
    Final fallback: 'application/octet-stream'.

    Args:
        content: Raw file bytes (first 8KB is sufficient for detection).
        filename: Original filename; used for extension fallback and logging.

    Returns:
        MIME type string, never None or empty.
    """
    try:
        import magic  # type: ignore[import-untyped]

        # Use first 8KB for detection (matches libmagic default)
        sample = content[:8192]
        mime = magic.from_buffer(sample, mime=True)
        if mime:
            return mime
        log.warning("magic_empty_result", filename=filename)
    except ImportError:
        log.debug("python_magic_unavailable", filename=filename)
    except Exception as exc:
        log.warning("magic_detection_failed", filename=filename, error=str(exc))

    # Extension-based fallback when libmagic is not available
    if filename:
        ext = os.path.splitext(filename.lower())[1]
        if ext in _EXT_MAP:
            log.debug("content_type_from_extension", filename=filename, ext=ext)
            return _EXT_MAP[ext]

    return _FALLBACK

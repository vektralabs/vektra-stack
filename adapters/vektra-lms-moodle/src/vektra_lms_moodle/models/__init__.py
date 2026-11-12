"""Strict per-version Moodle payload models; ``adapters_for`` dispatches on the release branch."""

from __future__ import annotations

import re
from typing import Any

from pydantic import TypeAdapter

from vektra_lms_moodle.models import v4_3, v5_2

SUPPORTED_BRANCHES: dict[str, dict[str, TypeAdapter[Any]]] = {
    "4.3": v4_3.ADAPTERS,
    "5.2": v5_2.ADAPTERS,
}

_RELEASE = re.compile(r"^(\d+)\.(\d+)(?:\.|\b)")


def branch_of(release: str | None) -> str | None:
    """``"4.3.11+ (Build: 20250411)"`` -> ``"4.3"``; None for an unknown or malformed release."""
    if not release:
        return None
    m = _RELEASE.match(release)
    if m is None:
        return None
    branch = f"{m.group(1)}.{m.group(2)}"
    return branch if branch in SUPPORTED_BRANCHES else None

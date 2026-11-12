"""Content-free shape evidence for a Moodle response (ARCH-070 ``evidence_hash``).

The signature lists keys, value types and list lengths, never values, so it can be stored
and logged without carrying a student's data. ``evidence_hash`` of a probe is the sha256
of this signature plus the release.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from vektra_lms_moodle.result import ShapeEvidence


def shape_signature(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: shape_signature(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        # the element shape of the first item plus the count: enough to see a change
        return ["list", len(value), shape_signature(value[0]) if value else None]
    return type(value).__name__


def evidence_for(wsfunction: str, release: str | None, doc: Any) -> ShapeEvidence:
    canonical = json.dumps(
        [wsfunction, release, shape_signature(doc)],
        sort_keys=True,
        separators=(",", ":"),
    )
    return ShapeEvidence(
        wsfunction, release, hashlib.sha256(canonical.encode()).hexdigest()
    )

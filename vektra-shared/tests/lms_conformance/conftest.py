"""Plumbing of the conformance kit (plan task 2a-1.7).

The kit is consumed from other test trees (the vertical's fake provider today, the provider's own
repository after phase 2b), so it is imported as the package ``lms_conformance``: this puts its
parent directory on ``sys.path`` for every invocation style, as the vertical's ``conftest.py``
does for its helpers. A consumer does the same once (``KIT_PARENT``).

What a consumer supplies is a ``kit.ConformanceTarget``: the **provider factory** (one fresh
provider per ``kit.TransportMode``) over its **recorded transport**, the ids of that recording,
the canaries planted in it and the credentials it uses. The suite (``suite.py``) does the rest.
"""

from __future__ import annotations

import sys
from pathlib import Path

KIT_PARENT = str(Path(__file__).resolve().parents[1])
if KIT_PARENT not in sys.path:
    sys.path.insert(0, KIT_PARENT)

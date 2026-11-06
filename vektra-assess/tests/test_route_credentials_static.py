"""M2.7 / ADR-0030 (static half): no assess API handler reads credentials from the query string
or from cookies. The dynamic half (every mounted route refuses a valid token passed that way) is
``tests/integration/test_route_credentials.py``. Shared with plan 9's inventory test.
"""

from __future__ import annotations

import re
from pathlib import Path

import vektra_assess.api as api_pkg

API_DIR = Path(api_pkg.__file__).resolve().parent

#: any read of the query string or of cookies
FORBIDDEN = re.compile(
    r"\.query_params\b|\.cookies\b|\bCookie\(|\bQuery\(|APIKeyQuery|APIKeyCookie"
)


def test_no_api_module_reads_query_params_or_cookies() -> None:
    offenders = [
        f"{p.name}:{n}: {line.strip()}"
        for p in sorted(API_DIR.glob("*.py"))
        for n, line in enumerate(p.read_text().splitlines(), start=1)
        if FORBIDDEN.search(line)
    ]
    assert offenders == [], offenders


def test_the_only_credential_source_is_the_authorization_header() -> None:
    deps = (API_DIR / "deps.py").read_text()
    assert 'request.headers.get("authorization"' in deps

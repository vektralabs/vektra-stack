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


#: (module, function) allowed to look at query parameter NAMES, only to refuse the request:
#: the plan-10 resend rejects any query (a recipient there is ERR-ASSESS-019, REQ-082)
REFUSERS = {("transcripts.py", "_refuse_query")}


def _function_of(lines: list[str], n: int) -> str | None:
    for line in reversed(lines[:n]):
        m = re.match(r"^(?:async )?def (\w+)", line)
        if m:
            return m.group(1)
    return None


def test_no_api_module_reads_query_params_or_cookies() -> None:
    offenders = []
    for p in sorted(API_DIR.glob("*.py")):
        lines = p.read_text().splitlines()
        for n, line in enumerate(lines, start=1):
            if FORBIDDEN.search(line):
                if (
                    p.name,
                    _function_of(lines, n),
                ) in REFUSERS and ".cookies" not in line:
                    continue
                offenders.append(f"{p.name}:{n}: {line.strip()}")
    assert offenders == [], offenders


def test_the_query_refuser_only_refuses() -> None:
    """The one allowed reader raises on any query and returns nothing (no value escapes)."""
    import ast

    src = (API_DIR / "transcripts.py").read_text()
    fn = next(
        f
        for f in ast.walk(ast.parse(src))
        if isinstance(f, ast.FunctionDef) and f.name == "_refuse_query"
    )
    returns = [
        r for r in ast.walk(fn) if isinstance(r, ast.Return) and r.value is not None
    ]
    assert returns == [] and sum(isinstance(r, ast.Raise) for r in ast.walk(fn)) == 2
    assert ".values()" not in ast.unparse(fn) and ".get(" not in ast.unparse(fn)


def test_the_only_credential_source_is_the_authorization_header() -> None:
    deps = (API_DIR / "deps.py").read_text()
    assert 'request.headers.get("authorization"' in deps

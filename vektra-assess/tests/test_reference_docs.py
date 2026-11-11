"""The assess reference docs match the code (plan assess-hardening-release, 13b).

Three registries are documented by hand and drift silently: the error codes, the settings and
the routes. Each test compares one of them with the code, in both directions where the docs can
name something the code does not have.

- every ``ERR-ASSESS-*`` constant of ``vektra_shared.errors`` has a row in
  ``docs/reference/error-codes.md`` with its HTTP status, every row names a registered code, and
  every ``ERR-ASSESS-*`` the source mentions is registered;
- every ``VektraAssessSettings`` variable is in ``docs/reference/configuration.md`` and in
  ``.env.example``, and every assess variable those two files name is read by the code;
- every mounted assess route is in the route index of ``docs/reference/api.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ERROR_DOC = ROOT / "docs" / "reference" / "error-codes.md"
CONFIG_DOC = ROOT / "docs" / "reference" / "configuration.md"
API_DOC = ROOT / "docs" / "reference" / "api.md"
ENV_EXAMPLE = ROOT / ".env.example"

_CODE = re.compile(r"ERR-ASSESS-\d{3}")
_VAR = re.compile(r"VEKTRA_ASSESS_[A-Z0-9_]*[A-Z0-9]")


def _registered() -> dict[str, int]:
    from vektra_shared import errors

    codes = {
        value
        for name, value in vars(errors).items()
        if name.startswith("ERR_ASSESS_") and isinstance(value, str)
    }
    overrides = errors._CODE_STATUS_OVERRIDE
    missing = sorted(c for c in codes if c not in overrides)
    assert not missing, f"assess codes without an HTTP status: {missing}"
    return {c: overrides[c] for c in codes}


def _doc_rows() -> dict[str, int]:
    rows: dict[str, int] = {}
    for line in ERROR_DOC.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and _CODE.fullmatch(cells[0]):
            assert cells[0] not in rows, f"{cells[0]} has two rows"
            rows[cells[0]] = int(cells[2])
    return rows


def test_every_assess_error_code_is_documented_with_its_status() -> None:
    registered, documented = _registered(), _doc_rows()
    assert sorted(set(registered) - set(documented)) == [], "codes without a row"
    assert sorted(set(documented) - set(registered)) == [], "rows for unknown codes"
    wrong = {
        c: (documented[c], registered[c])
        for c in registered
        if documented[c] != registered[c]
    }
    assert wrong == {}, "documented status != registry (doc, code)"


def test_every_assess_code_the_source_mentions_is_registered() -> None:
    registered = set(_registered())
    seen: dict[str, str] = {}
    for base in ("vektra-assess/src", "vektra-app/src", "vektra-shared/src"):
        for path in (ROOT / base).rglob("*.py"):
            for code in _CODE.findall(path.read_text(encoding="utf-8")):
                seen.setdefault(code, str(path.relative_to(ROOT)))
    unknown = {c: p for c, p in seen.items() if c not in registered}
    assert unknown == {}


def _aliases() -> set[str]:
    from vektra_assess.config import VektraAssessSettings

    return {f.alias for f in VektraAssessSettings.model_fields.values() if f.alias}


@pytest.mark.parametrize("doc", [CONFIG_DOC, ENV_EXAMPLE], ids=lambda p: p.name)
def test_every_assess_setting_is_documented(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    missing = sorted(
        a for a in _aliases() if not re.search(rf"\b{re.escape(a)}\b", text)
    )
    assert missing == [], f"{doc.name} does not name: {missing}"


def _read_elsewhere() -> tuple[set[str], tuple[str, ...]]:
    """Assess variables read outside ``VektraAssessSettings``: the worker policy's families
    (``vektra_assess.worker.policy``) and the judge of ``make eval-assess``."""
    from vektra_assess.worker import policy

    eval_script = (ROOT / "tests" / "eval" / "eval_assess.py").read_text(
        encoding="utf-8"
    )
    names = {policy._PREFLIGHT_ALIAS, *_VAR.findall(eval_script)}
    prefixes = (policy._POOL_PREFIX, policy._JOB_PREFIX)
    return names, prefixes


@pytest.mark.parametrize("doc", [CONFIG_DOC, ENV_EXAMPLE], ids=lambda p: p.name)
def test_no_documented_assess_variable_is_unknown_to_the_code(doc: Path) -> None:
    names, prefixes = _read_elsewhere()
    known = _aliases() | names
    stale = sorted(
        v
        for v in set(_VAR.findall(doc.read_text(encoding="utf-8")))
        if v not in known
        # ``VEKTRA_ASSESS_JOB_<KIND>_...`` is written as the bare family name in prose
        and not any((v + "_").startswith(p) for p in prefixes)
    )
    assert stale == [], f"{doc.name} names variables no code reads: {stale}"


def _routes() -> set[tuple[str, str]]:
    from fastapi import FastAPI

    from vektra_assess.api.router import router
    from vektra_assess.ui.router import router as ui

    app = FastAPI()
    app.include_router(router)
    app.include_router(ui)
    return {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        if path.startswith("/api/v1/assess")
        for method in ops
    }


def _route_index() -> set[tuple[str, str]]:
    text = API_DOC.read_text(encoding="utf-8")
    start = text.index("**Route index.**")
    end = text.index("**Event stream wire format**", start)
    rows: set[tuple[str, str]] = set()
    for line in text[start:end].splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[1].startswith("`/"):
            continue
        for method in cells[0].split(","):
            rows.add((method.strip().upper(), cells[1].strip("`")))
    return rows


def _shape(path: str) -> str:
    """Path parameters compared by position, not by name."""
    return re.sub(r"\{[^}]+\}", "{}", path)


def test_every_assess_route_is_in_the_api_route_index() -> None:
    code = {(m, _shape(p)) for m, p in _routes()}
    doc = {(m, _shape(p)) for m, p in _route_index()}
    assert sorted(code - doc) == [], "mounted routes missing from the index"
    assert sorted(doc - code - {("GET", "/health/assess")}) == [], (
        "index rows for routes that are not mounted"
    )


def test_the_route_index_is_not_empty() -> None:
    """False-pass guard: a moved marker must not turn the comparison into empty == empty."""
    assert len(_route_index()) >= 30
    assert len(_routes()) >= 30

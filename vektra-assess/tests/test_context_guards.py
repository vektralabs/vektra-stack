"""Structural guards for the context boundary (plan assess-context E, ARCH-069).

import-linter sees modules, not call sites. This AST scan finds, anywhere in the repository's
Python (product code AND tests), a construction of ``ContextBundle`` or one of its escape hatches,
the private sentinel, or an import of ``_assembly``, outside ``vektra_assess/context/``. The
allowlist is EMPTY. The only mentions it accepts are inside ``with
pytest.raises(BundleConstructionError):`` blocks, which prove the refusal.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "vektra-assess" / "src" / "vektra_assess" / "context"
HATCHES = {
    "model_construct",
    "model_validate",
    "model_validate_json",
    "model_validate_strings",
    "__new__",
}
PRIVATE = {"_KEY", "_SENTINEL"}


def _files(root: Path) -> list[Path]:
    out = []
    for pattern in ("vektra-*/src/**/*.py", "vektra-*/tests/**/*.py", "tests/**/*.py"):
        out += [p for p in root.glob(pattern) if ".venv" not in p.parts]
    return sorted(set(out))


def _guarded_lines(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "raises"
                and any(
                    isinstance(a, ast.Name) and a.id == "BundleConstructionError"
                    for a in call.args
                )
            ):
                for inner in node.body:
                    for sub in ast.walk(inner):
                        if hasattr(sub, "lineno"):
                            lines.add(sub.lineno)
    return lines


def violations(root: Path, package: Path) -> list[str]:
    bad = []
    for path in _files(root):
        if package in path.parents:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        ok = _guarded_lines(tree)
        rel = path.relative_to(root)
        for node in ast.walk(tree):
            line = getattr(node, "lineno", 0)
            if line in ok:
                continue
            if isinstance(node, ast.Call):
                f = node.func
                name = (
                    f.id
                    if isinstance(f, ast.Name)
                    else f.attr
                    if isinstance(f, ast.Attribute)
                    else ""
                )
                if name == "ContextBundle":
                    bad.append(f"{rel}:{line} ContextBundle(...)")
            if isinstance(node, ast.Attribute):
                if (
                    node.attr in HATCHES
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "ContextBundle"
                ):
                    bad.append(f"{rel}:{line} ContextBundle.{node.attr}")
                if node.attr in PRIVATE:
                    bad.append(f"{rel}:{line} .{node.attr}")
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names = {a.name for a in node.names}
                if mod.endswith("context._assembly") or (
                    mod.endswith("vektra_assess.context") and "_assembly" in names
                ):
                    bad.append(f"{rel}:{line} import _assembly")
                if mod.endswith("context.bundle") and names & PRIVATE:
                    bad.append(f"{rel}:{line} import {sorted(names & PRIVATE)}")
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.endswith("context._assembly"):
                        bad.append(f"{rel}:{line} import _assembly")
    return bad


def test_no_bundle_construction_outside_the_package() -> None:
    assert len(_files(ROOT)) > 200, "the scan really covers the repository"
    assert violations(ROOT, PACKAGE) == []


@pytest.mark.parametrize(
    "code",
    [
        "from vektra_assess.context.bundle import ContextBundle\nContextBundle(activity_kind='quiz')\n",
        "from vektra_assess.context.bundle import ContextBundle\nContextBundle.model_construct()\n",
        "import vektra_assess.context.bundle as b\nb._KEY[0]\n",
        "from vektra_assess.context import _assembly\n",
        "from vektra_assess.context._assembly import assemble\n",
        "from vektra_assess.context.bundle import _KEY\n",
        "import pytest\nfrom vektra_assess.context.bundle import ContextBundle\nwith pytest.raises(ValueError):\n    ContextBundle()\n",
    ],
)
def test_the_guard_catches_a_planted_violation(tmp_path: Path, code: str) -> None:
    (tmp_path / "vektra-x" / "src").mkdir(parents=True)
    (tmp_path / "vektra-x" / "src" / "planted.py").write_text(code)
    assert violations(tmp_path, tmp_path / "none") != []


def test_a_refusal_check_is_not_a_violation(tmp_path: Path) -> None:
    (tmp_path / "vektra-x" / "tests").mkdir(parents=True)
    (tmp_path / "vektra-x" / "tests" / "t.py").write_text(
        "import pytest\nfrom vektra_assess.context.bundle import BundleConstructionError, ContextBundle\n"
        "with pytest.raises(BundleConstructionError):\n    ContextBundle()\n"
    )
    assert violations(tmp_path, tmp_path / "none") == []


# --- typing: the model adapter accepts ContextBundle only (ARCH-074) --------------------------------

TYPING_CASE = """
from vektra_assess.context.payload import bundle_to_model_payload
from vektra_assess.context.snapshot import Snapshot


def wrong(snapshot: Snapshot, raw: dict[str, object]) -> None:
    bundle_to_model_payload(snapshot)  # must not type-check
    bundle_to_model_payload(raw)  # must not type-check
"""


def test_passing_a_snapshot_or_a_dict_does_not_type_check(tmp_path: Path) -> None:
    """Plan 7's runner signature must follow; today the single serializer carries the type."""
    case = tmp_path / "typing_case.py"
    case.write_text(TYPING_CASE)
    # the sources, not the installed packages (they carry no py.typed): as CI's mypy step sees them
    srcs = [str(ROOT / d / "src") for d in ("vektra-assess", "vektra-shared")]
    env = {**os.environ, "MYPYPATH": os.pathsep.join(srcs)}
    res = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental", "--strict", "--follow-imports=silent", str(case)],
        capture_output=True, text=True, cwd=ROOT, timeout=300, env=env,
    )  # fmt: skip
    assert res.returncode == 1, res.stdout + res.stderr
    errors = [
        ln for ln in res.stdout.splitlines() if ": error:" in ln and "arg-type" in ln
    ]
    assert len(errors) == 2, res.stdout

"""Module boundaries of vektra_assess (plan assess-foundations 9.1, ARCH-073).

- the contract (c) source list covers every assess sub-package/module except worker,
  moodle and parser_client, so a new one cannot slip outside it;
- (d) ``ContextBundle`` is constructed only in ``vektra_assess.context`` (AST scan;
  import-linter cannot express it);
- canaries: a forbidden import planted in a temporary copy of the sources makes
  ``lint-imports`` fail, so the contracts are proven to bite, not just to pass.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "vektra-assess" / "src" / "vektra_assess"
WORKER_ONLY = {"moodle"}
WORKER_ONLY_MODULES = {"moodle", "worker.parser_client"}
CONTRACT_C = "only vektra_assess.worker imports vektra_assess.moodle and parser_client"


def _contracts() -> dict[str, dict]:  # type: ignore[type-arg]
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {c["name"]: c for c in cfg["tool"]["importlinter"]["contracts"]}


def _children() -> set[str]:
    out = set()
    for p in PKG.iterdir():
        if p.name.startswith("_"):
            continue
        if p.is_dir() and (p / "__init__.py").exists():
            out.add(p.name)
        elif p.suffix == ".py":
            out.add(p.stem)
    return out


def test_contract_c_covers_every_child() -> None:
    sources = {
        m.removeprefix("vektra_assess.")
        for m in _contracts()[CONTRACT_C]["source_modules"]
    }
    expected = _children() - {"worker"} - WORKER_ONLY
    assert sources == expected, (
        f"missing from contract (c): {sorted(expected - sources)}; "
        f"stale: {sorted(sources - expected)}"
    )
    assert {
        m.removeprefix("vektra_assess.")
        for m in _contracts()[CONTRACT_C]["forbidden_modules"]
    } == WORKER_ONLY_MODULES


PARSER_CONTRACT = "vektra_assess.parser_sidecar imports only parser_protocol"
CONTRACT_CTX = "only vektra_assess.worker imports vektra_assess.context.capture"
CONTRACT_ASSEMBLY = (
    "vektra_assess.context._assembly is private to vektra_assess.context"
)
CONTRACT_CTX_DEPS = (
    "vektra_assess.context imports no retrieval stack and no HTTP client"
)

CONTRACT_LLM_DEPS = "vektra_assess.llm imports no retrieval, analytics or ML stack"
CONTRACT_LLM_CALLERS = (
    "only vektra_assess.api and vektra_assess.worker import vektra_assess.llm"
)


def test_llm_callers_contract_covers_every_other_child() -> None:
    sources = {
        m.removeprefix("vektra_assess.")
        for m in _contracts()[CONTRACT_LLM_CALLERS]["source_modules"]
    }
    expected = _children() - {"api", "worker", "llm"}
    assert sources == expected, (
        f"missing: {sorted(expected - sources)}; stale: {sorted(sources - expected)}"
    )


def test_parser_contract_forbids_every_other_assess_child() -> None:
    forbidden = {
        m.removeprefix("vektra_assess.")
        for m in _contracts()[PARSER_CONTRACT]["forbidden_modules"]
        if m.startswith("vektra_assess.")
    }
    assert forbidden == _children() - {"parser_protocol", "parser_sidecar"}


def test_every_existing_contract_forbids_vektra_assess() -> None:
    for name, c in _contracts().items():
        if c["type"] != "forbidden" or "vektra_assess" in str(c["source_modules"]):
            continue
        if name in (
            "No component shall import the app entrypoint",
            "vektra_app.main must not import the parser entry",
        ):
            continue
        assert "vektra_assess" in c["forbidden_modules"], name


# --- (d) ContextBundle construction ------------------------------------------


def _construction_sites() -> tuple[bool, list[str]]:
    defined, sites = False, []
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(PKG).as_posix()
        in_context = rel.startswith("context/")
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ContextBundle":
                defined = True
            if in_context or not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.id if isinstance(f, ast.Name) else None
            if isinstance(f, ast.Attribute):
                owner = f.value.id if isinstance(f.value, ast.Name) else None
                if f.attr == "ContextBundle" or (
                    owner == "ContextBundle"
                    and f.attr
                    in {"__new__", "model_construct", "model_validate", "construct"}
                ):
                    name = "ContextBundle"
            if name == "ContextBundle":
                sites.append(f"{rel}:{node.lineno}")
    return defined, sites


def test_contextbundle_constructed_only_in_context() -> None:
    defined, sites = _construction_sites()
    # a scan that never finds the class definition proves nothing about its call sites
    assert defined, "ContextBundle definition not found under vektra_assess.context"
    assert sites == [], (
        f"ContextBundle constructed outside vektra_assess.context: {sites}"
    )


# --- canaries: the contracts bite ----------------------------------------------


def _copy_sources(dst: Path) -> list[Path]:
    srcs = []
    for pkg_dir in sorted(ROOT.glob("vektra-*/src")):
        target = dst / pkg_dir.parent.name / "src"
        shutil.copytree(pkg_dir, target, ignore=shutil.ignore_patterns("__pycache__"))
        srcs.append(target)
    shutil.copy(ROOT / "pyproject.toml", dst / "pyproject.toml")
    return srcs


def _lint_imports(dst: Path, srcs: list[Path]) -> subprocess.CompletedProcess[str]:
    lint = shutil.which("lint-imports") or str(
        Path(sys.executable).parent / "lint-imports"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(s) for s in srcs)}
    return subprocess.run(
        [lint, "--config", str(dst / "pyproject.toml"), "--no-cache"],
        cwd=dst,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.parametrize(
    ("module", "code", "broken"),
    [
        (
            "api/_canary.py",
            "import vektra_core\n",
            "vektra_assess must not import from other vektra components",
        ),
        ("api/_canary.py", "import vektra_assess.moodle\n", CONTRACT_C),
        # the capture split (plan assess-context): the API-importable half of context may not
        # reach Moodle, and nothing but the worker may import capture
        ("context/_canary.py", "import vektra_assess.moodle\n", CONTRACT_C),
        (
            "context/_canary.py",
            "import vektra_assess.context.capture.selection\n",
            CONTRACT_CTX,
        ),
        ("api/_canary.py", "import vektra_assess.context.capture\n", CONTRACT_CTX),
        (
            "api/_canary.py",
            "import vektra_assess.context._assembly\n",
            CONTRACT_ASSEMBLY,
        ),
        (
            "worker/_canary.py",
            "from vektra_assess.context import _assembly\n",
            CONTRACT_ASSEMBLY,
        ),
        ("context/_canary.py", "import httpx\n", CONTRACT_CTX_DEPS),
        ("context/capture/_canary.py", "import httpx\n", CONTRACT_CTX_DEPS),
        (
            "api/_canary.py",
            "import vektra_assess.worker.parser_client\n",
            CONTRACT_C,
        ),
        (
            "parser_sidecar/_canary.py",
            "import vektra_assess.config\n",
            PARSER_CONTRACT,
        ),
        ("parser_sidecar/_canary.py", "import sqlalchemy\n", PARSER_CONTRACT),
        (
            "parser_sidecar/_canary.py",
            "import vektra_assess.worker.parser_client\n",
            PARSER_CONTRACT,
        ),
        ("llm/_canary.py", "import vektra_analytics\n", CONTRACT_LLM_DEPS),
        ("llm/_canary.py", "import qdrant_client\n", CONTRACT_LLM_DEPS),
        ("context/_canary.py", "import vektra_assess.llm\n", CONTRACT_LLM_CALLERS),
        ("delivery/_canary.py", "import vektra_assess.llm\n", CONTRACT_LLM_CALLERS),
        (
            "llm/_canary.py",
            "import torch\n",
            "vektra_assess must not import the ML, vector store or parsing stacks",
        ),
    ],
)
def test_planted_forbidden_import_breaks_lint_imports(
    tmp_path: Path, module: str, code: str, broken: str
) -> None:
    # import-linter is a root dev dependency (make lint, CI): a missing tool is a broken
    # environment, not a reason to stop proving the contracts bite
    assert (
        shutil.which("lint-imports")
        or (Path(sys.executable).parent / "lint-imports").exists()
    ), "lint-imports is not installed (uv sync --dev)"
    srcs = _copy_sources(tmp_path)
    clean = _lint_imports(tmp_path, srcs)
    assert clean.returncode == 0, (
        clean.stdout + clean.stderr
    )  # the copy itself is clean
    (tmp_path / "vektra-assess" / "src" / "vektra_assess" / module).write_text(code)
    res = _lint_imports(tmp_path, srcs)
    assert res.returncode != 0
    assert f"{broken} BROKEN" in res.stdout, res.stdout

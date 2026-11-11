"""Assess modules log through the allowlist (plan assess-context A)."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest
import structlog

SRC = Path(__file__).resolve().parents[1] / "src" / "vektra_assess"
CANARY = "CNRY-solution_text-essay-0f0f0f0f"


def _calls(tree: ast.AST, attr: str, owner: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == attr
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == owner
    ]


def _violations(src: Path = SRC) -> list[str]:
    bad = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text())
        # logging.getLogger().handlers reads the root's handlers (to flush them): no logging there
        reads = {
            id(n.value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "handlers"
        }
        for call in _calls(tree, "get_logger", "structlog"):
            # a bare get_logger() has no name, so the allowlist cannot tell it is an assess logger
            if not call.args:
                bad.append(
                    f"{path.relative_to(src)}:{call.lineno} structlog.get_logger()"
                )
        for call in _calls(tree, "getLogger", "logging"):
            if id(call) in reads:
                continue
            arg = call.args[0] if call.args else None
            # the same predicate as is_assess_logger: "vektra_assessment" is not an assess name
            name = str(arg.value) if isinstance(arg, ast.Constant) else ""
            named = name == "vektra_assess" or name.startswith("vektra_assess.")
            # a bare getLogger() is the root: its records skip the assess filter
            if not named and not isinstance(arg, ast.Name):
                bad.append(
                    f"{path.relative_to(src)}:{call.lineno} logging.getLogger(...)"
                )
    return bad


_LEVELS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "msg"}
)


#: (file, function) whose message is built on purpose. The parser sidecar runs in its own process
#: with its own stderr (no assess filter there) and writes one JSON object of fixed operational
#: keys per event (codes, counts, pids): no file content ever reaches it.
_MESSAGE_EXEMPT = {("parser_sidecar/server.py", "_event")}


def _message_violations(src: Path = SRC) -> list[str]:
    """The message of every log call is a literal event name, a %-template or a module constant.

    The runtime filter keeps a short message as is: it cannot tell a constant event name from an
    f-string that interpolates content, so the rule is enforced here, at the call site."""
    bad = []
    for path in sorted(src.rglob("*.py")):
        rel = str(path.relative_to(src))
        tree = ast.parse(path.read_text())
        exempt = {
            id(n)
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
            and (rel, fn.name) in _MESSAGE_EXEMPT
            for n in ast.walk(fn)
        }
        for call in ast.walk(tree):
            if id(call) in exempt or not (
                isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            ):
                continue
            # the logger is a name (log.info) or an attribute (self._log.info)
            owner = call.func.value
            if isinstance(owner, ast.Name):
                owner_name = owner.id
            elif isinstance(owner, ast.Attribute):
                owner_name = owner.attr
            else:
                continue
            if "log" not in owner_name.lower():
                continue
            attr = call.func.attr
            if attr in _LEVELS:
                msg = call.args[0] if call.args else None
            elif attr == "log":
                msg = call.args[1] if len(call.args) > 1 else None
            else:
                continue
            if msg is None:
                continue  # no positional message (event=... keyword): nothing interpolated
            literal = isinstance(msg, ast.Constant) and isinstance(msg.value, str)
            constant = isinstance(msg, ast.Name) and msg.id.isupper()
            if not (literal or constant):
                bad.append(f"{rel}:{call.lineno} {attr}(<non-literal>)")
    return bad


def test_every_assess_logger_carries_an_assess_name() -> None:
    assert _violations() == []


def test_every_assess_log_message_is_a_literal() -> None:
    assert _message_violations() == []


def test_the_message_guard_catches_interpolated_messages(tmp_path: Path) -> None:
    pkg = tmp_path / "vektra_assess"
    pkg.mkdir()
    (pkg / "planted.py").write_text(
        "import logging\n"
        "log = logging.getLogger(__name__)\n"
        "def f(x):\n"
        '    log.warning(f"bad {x}")\n'
        "    log.info(\n"
        '        "a " + x,\n'
        "    )\n"
        '    log.log(30, "t %s".format(x))\n'
        '    log.info("ok %s", x)\n'
        "class C:\n"
        "    def g(self, x):\n"
        '        self._log.error(f"bad {x}")\n'
    )
    assert _message_violations(pkg) == [
        "planted.py:4 warning(<non-literal>)",
        "planted.py:5 info(<non-literal>)",
        "planted.py:8 log(<non-literal>)",
        "planted.py:12 error(<non-literal>)",
    ]


def test_the_guard_catches_a_bare_logger(tmp_path: Path) -> None:
    pkg = tmp_path / "vektra_assess"
    pkg.mkdir()
    (pkg / "planted.py").write_text("import structlog\nlog = structlog.get_logger()\n")
    assert _violations(pkg) == ["planted.py:2 structlog.get_logger()"]


@pytest.mark.parametrize(
    "call", ["logging.getLogger()", 'logging.getLogger("vektra_assessment")']
)
def test_the_guard_catches_a_root_or_lookalike_stdlib_logger(
    tmp_path: Path, call: str
) -> None:
    pkg = tmp_path / "vektra_assess"
    pkg.mkdir()
    (pkg / "planted.py").write_text(f"import logging\nlog = {call}\n")
    assert _violations(pkg) == ["planted.py:2 logging.getLogger(...)"]


def test_real_module_loggers_are_filtered(canary_log_capture) -> None:  # type: ignore[no-untyped-def]
    from vektra_assess.moodle import client  # a module that logs through structlog

    client.log.warning(
        "assess_probe", wsfunction="core_webservice_get_site_info", body=CANARY
    )
    logging.getLogger("vektra_assess.worker").info(
        "assess_tick", extra={"payload": CANARY}
    )
    structlog.get_logger("vektra_core.x").info("not_assess")
    canary_log_capture.assert_clean([CANARY])
    assert len(canary_log_capture.lines()) == 3


def test_capture_refuses_an_empty_sink(canary_log_capture) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(AssertionError, match="log sink is empty"):
        canary_log_capture.assert_clean([CANARY])


def test_capture_catches_extra_rendered_output(canary_log_capture) -> None:  # type: ignore[no-untyped-def]
    canary_log_capture.extra_text.append(f"sidecar stderr: {CANARY}\n")
    with pytest.raises(AssertionError, match="canary hit"):
        canary_log_capture.assert_clean([CANARY])


# --- the strict prefix is registered by the package itself (plan 2a-1.4, ADR-0033 D8) --------


def _prefixes_after(imports: str) -> str:
    import subprocess
    import sys

    code = (
        f"{imports}; from vektra_shared.log_safety import strict_logger_prefixes; "
        "print(','.join(strict_logger_prefixes()))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def test_log_safety_alone_hard_codes_no_prefix() -> None:
    """Nothing in vektra_shared names the vertical: the strictness is the package's to claim."""
    assert _prefixes_after("import vektra_shared.log_safety") == ""


def test_importing_vektra_assess_registers_its_prefix() -> None:
    assert _prefixes_after("import vektra_assess") == "vektra_assess"


def test_a_leaf_module_import_registers_it_too() -> None:
    """Any import of any assess module runs the package ``__init__`` first."""
    assert _prefixes_after("import vektra_assess.capabilities") == "vektra_assess"

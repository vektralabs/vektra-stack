#!/usr/bin/env python3
"""Generate the strict per-version Pydantic models of ``vektra_assess.moodle.models``.

The models mirror the REAL payloads of the recorded fixtures
(``vektra-assess/tests/fixtures/moodle_ws/<branch>/``, written by ``assess_record_ws.py``):
every key observed in any recording of a wsfunction becomes a field; a key missing from some
recordings is optional (Moodle omits ``VALUE_OPTIONAL`` keys); a key observed with ``null`` is
nullable. ``extra="forbid"`` makes any key a future point release adds an ``unknown_shape``
on purpose (ARCH-070: fail closed, re-record, regenerate, review the diff).

    uv run python scripts/assess_gen_ws_models.py
"""

from __future__ import annotations

import json
import keyword
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent / "vektra-assess"
FIXTURES = ROOT / "tests" / "fixtures" / "moodle_ws"
MODELS = ROOT / "src" / "vektra_assess" / "moodle" / "models"

ROOTS = {
    "core_webservice_get_site_info": "SiteInfoDoc",
    "mod_quiz_get_quizzes_by_courses": "QuizzesByCourses",
    "mod_quiz_get_overrides": "QuizOverrides",
    "mod_quiz_get_user_attempts": "UserAttempts",
    "mod_quiz_get_attempt_review": "AttemptReviewDoc",
    "mod_quiz_get_combined_review_options": "CombinedReviewOptions",
    "mod_assign_get_assignments": "Assignments",
    "mod_assign_get_user_flags": "UserFlagsDoc",
    "mod_assign_get_participant": "Participant",
    "mod_assign_get_submission_status": "SubmissionStatusDoc",
    "gradereport_user_get_grade_items": "GradeItems",
    "core_grading_get_definitions": "GradingDefinitions",
    "core_grading_get_gradingform_instances": "GradingInstances",
    "core_enrol_get_users_courses": "UsersCourses",
    "core_enrol_get_enrolled_users": "EnrolledUsers",
    "core_enrol_get_enrolled_users_with_capability": "UsersWithCapability",
}
FILES = {
    "core_webservice_get_site_info": "site",
    "mod_quiz_get_quizzes_by_courses": "quiz",
    "mod_quiz_get_overrides": "quiz",
    "mod_quiz_get_user_attempts": "quiz",
    "mod_quiz_get_attempt_review": "quiz",
    "mod_quiz_get_combined_review_options": "quiz",
    "mod_assign_get_assignments": "assign",
    "mod_assign_get_user_flags": "assign",
    "mod_assign_get_participant": "assign",
    "mod_assign_get_submission_status": "assign",
    "gradereport_user_get_grade_items": "assign",
    "core_grading_get_definitions": "grading",
    "core_grading_get_gradingform_instances": "grading",
    "core_enrol_get_users_courses": "enrol",
    "core_enrol_get_enrolled_users": "enrol",
    "core_enrol_get_enrolled_users_with_capability": "enrol",
}
WARNING = "Warning"
#: Moodle numbers that are float on some rows and int on others (the recordings show one).
FLOATISH = {
    "maxmark",
    "sumgrades",
    "grade",
    "grademax",
    "grademin",
    "graderaw",
    "weightraw",
}
#: fields Moodle declares NULL_ALLOWED although the recordings only show a value
NULLABLE = {
    ("QuizOverridesOverridesItem", f)
    for f in (
        "timeopen",
        "timeclose",
        "timelimit",
        "attempts",
        "password",
        "groupid",
        "userid",
    )
}


class Node:
    """The merged shape of every observed value at one path."""

    def __init__(self) -> None:
        self.types: set[str] = set()
        self.fields: dict[str, Node] = {}
        self.seen: dict[str, int] = defaultdict(int)
        self.samples = 0
        self.items: Node | None = None

    def add(self, value: Any) -> None:
        if value is None:
            self.types.add("null")
        elif isinstance(value, bool):
            self.types.add("bool")
        elif isinstance(value, int):
            self.types.add("int")
        elif isinstance(value, float):
            self.types.add("float")
        elif isinstance(value, str):
            self.types.add("str")
        elif isinstance(value, dict):
            self.types.add("obj")
            self.samples += 1
            for k, v in value.items():
                self.seen[k] += 1
                self.fields.setdefault(k, Node()).add(v)
        elif isinstance(value, list):
            self.types.add("list")
            self.items = self.items or Node()
            for v in value:
                self.items.add(v)


def camel(path: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[^A-Za-z0-9]+", path) if p)


class Emitter:
    def __init__(self) -> None:
        self.classes: list[str] = []

    def type_of(self, node: Node, name: str, key: str) -> str:
        if key == "warnings":
            return f"list[{WARNING}]"
        parts: list[str] = []
        nullable = "null" in node.types
        for t in sorted(node.types - {"null"}):
            if t == "obj":
                parts.append(self.emit(node, name))
            elif t == "list":
                inner = node.items
                if inner is None or not inner.types:
                    parts.append("list[Any]")
                else:
                    parts.append(f"list[{self.type_of(inner, name + 'Item', key)}]")
            elif t == "int" and key in FLOATISH:
                parts.append("float | int")
            else:
                parts.append(t)
        if not parts:
            return "Any"
        out = " | ".join(parts)
        return out + (" | None" if nullable else "")

    def emit(self, node: Node, name: str) -> str:
        lines = [f"class {name}(_Strict):"]
        if not node.fields:
            lines.append("    pass")
        for key, child in node.fields.items():
            ident = key
            alias = ""
            if (
                not key.isidentifier()
                or keyword.iskeyword(key)
                or key.startswith("model_")
            ):
                ident = re.sub(r"\W", "_", key) + "_"
                alias = f"alias={key!r}"
            typ = self.type_of(child, name + camel(key), key)
            if (name, key) in NULLABLE:
                typ = "Any" if typ == "Any" else typ.removesuffix(" | None") + " | None"
            optional = node.seen[key] < node.samples
            if optional and "None" not in typ.split(" | ") and typ != "Any":
                typ += " | None"
            default = ""
            if optional or alias:
                args = ([alias] if alias else []) + (
                    ["default=None"] if optional else []
                )
                default = f" = Field({', '.join(args)})" if alias else " = None"
            lines.append(f"    {ident}: {typ}{default}")
        self.classes.append("\n".join(lines))
        return name


def build(branch: str) -> None:
    per_fn: dict[str, Node] = {}
    for f in sorted((FIXTURES / branch).glob("*__*.json")):
        doc = json.loads(f.read_text())
        fn = doc["wsfunction"]
        if fn not in ROOTS:
            continue
        resp = doc["response"]
        if isinstance(resp, dict) and "exception" in resp:
            continue
        per_fn.setdefault(fn, Node()).add(resp)
    by_file: dict[str, list[str]] = defaultdict(list)
    for fn in per_fn:
        by_file[FILES[fn]].append(fn)
    pkg = MODELS / f"v{branch.replace('.', '_')}"
    pkg.mkdir(parents=True, exist_ok=True)
    registry: list[str] = []
    for mod, fns in sorted(by_file.items()):
        em = Emitter()
        reg: list[str] = []
        for fn in sorted(fns):
            node = per_fn[fn]
            rootname = ROOTS[fn]
            if node.types == {"list"} or node.types == {"list", "null"}:
                assert node.items is not None
                item = em.type_of(node.items, rootname + "Item", "")
                reg.append(f'    "{fn}": TypeAdapter(list[{item}]),')
            else:
                em.emit(node, rootname)
                reg.append(f'    "{fn}": TypeAdapter({rootname}),')
        uses_warning = any(f"list[{WARNING}]" in c for c in em.classes)
        header = [
            f'"""Strict models of the Moodle {branch} payloads ({mod}). GENERATED by',
            "``scripts/assess_gen_ws_models.py`` from the recorded fixtures: do not edit by hand.",
            '"""',
            "",
            "from __future__ import annotations",
            "",
            "from typing import Any",
            "",
            "from pydantic import Field, TypeAdapter"
            if any("Field(" in c for c in em.classes)
            else "from pydantic import TypeAdapter",
            "",
            "from vektra_assess.moodle.models.base import Strict as _Strict",
        ]
        if uses_warning:
            header.append(f"from vektra_assess.moodle.models.base import {WARNING}")
        body = "\n\n\n".join(em.classes)
        regtxt = (
            "ADAPTERS: dict[str, TypeAdapter[Any]] = {\n" + "\n".join(reg) + "\n}\n"
        )
        (pkg / f"{mod}.py").write_text(
            "\n".join(header) + "\n\n\n" + body + "\n\n\n" + regtxt
        )
        registry.append(mod)
    init = [
        f'"""Moodle {branch}: strict payload models, one adapter per wsfunction."""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
        "from pydantic import TypeAdapter",
        "",
        *[
            f"from vektra_assess.moodle.models.v{branch.replace('.', '_')} import {m}"
            for m in sorted(registry)
        ],
        "",
        "ADAPTERS: dict[str, TypeAdapter[Any]] = {",
        *[f"    **{m}.ADAPTERS," for m in sorted(registry)],
        "}",
        "",
    ]
    (pkg / "__init__.py").write_text("\n".join(init))


def main() -> None:
    for branch in ("4.3", "5.2"):
        build(branch)


if __name__ == "__main__":
    main()

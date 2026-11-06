"""Vektra-shipped strings of the discussion, per language (REQ-098, REQ-100, BR-009).

Loaded once at import from ``strings/{it,en}.yaml`` and validated: every behaviour class has a core
sentence and an alternative clause in every language, and every other key is present. A missing
string is an import error, never a silent fallback. The plan-9 UI and the plan-P2 plugin read them
through ``get`` (a read-only view): Moodle strings cannot override them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import yaml

from vektra_assess.llm.postcheck.markers import CLASS_CODES

LANGUAGES = ("it", "en")
DIR = Path(__file__).parent / "strings"
_MISSING_KEYS = frozenset(
    {"value_unverified", "quote_unverified", "replaced_by_system"}
)


class ShippedStringsError(RuntimeError):
    """A shipped string file is missing a key or has the wrong shape."""


@dataclass(frozen=True)
class ClassText:
    core: str
    alternative: str


@dataclass(frozen=True)
class Shipped:
    language: str
    br009: str  # template: {grade}, {date}
    classes: Mapping[str, ClassText]
    missing: Mapping[str, str]
    unsupported_language: str
    default_support_text: str
    #: REQ-099 (b): the D3 sentence of the REQ-077 teacher help text (plan 11 displays it)
    oracle_disclosure: str

    def refusal(self, klass: str, support_text: str | None = None) -> str:
        """The fixed core sentence plus its alternative clause (W-DISTRESS: the support text)."""
        t = self.classes[klass]
        alt = t.alternative.format(
            support_text=support_text or self.default_support_text
        )
        return f"{t.core} {alt}"


def _text(doc: Mapping[str, object], key: str, where: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ShippedStringsError(f"{where}: '{key}' missing or empty")
    return value


def _load(language: str, directory: Path = DIR) -> Shipped:
    where = f"{language}.yaml"
    doc = yaml.safe_load((directory / where).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ShippedStringsError(f"{where}: not a mapping")
    classes = doc.get("classes")
    if not isinstance(classes, dict) or set(classes) != CLASS_CODES:
        raise ShippedStringsError(
            f"{where}: classes must be exactly {sorted(CLASS_CODES)}"
        )
    parsed = {
        code: ClassText(
            _text(body, "core", f"{where} {code}"),
            _text(body, "alternative", f"{where} {code}"),
        )
        for code, body in classes.items()
    }
    missing = doc.get("missing")
    if not isinstance(missing, dict) or set(missing) != _MISSING_KEYS:
        raise ShippedStringsError(
            f"{where}: missing must be exactly {sorted(_MISSING_KEYS)}"
        )
    return Shipped(
        language=language,
        br009=_text(doc, "br009", where),
        classes=MappingProxyType(parsed),
        missing=MappingProxyType({k: _text(missing, k, where) for k in missing}),
        unsupported_language=_text(doc, "unsupported_language", where),
        default_support_text=_text(doc, "default_support_text", where),
        oracle_disclosure=_text(doc, "oracle_disclosure", where),
    )


_ALL: Mapping[str, Shipped] = MappingProxyType(
    {lang: _load(lang) for lang in LANGUAGES}
)


def get(language: str) -> Shipped:
    """The shipped strings of a supported language; Italian for anything else (REQ-100)."""
    return _ALL.get(language, _ALL["it"])

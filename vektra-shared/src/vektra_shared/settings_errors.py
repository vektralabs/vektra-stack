"""One explanation of a settings ``ValidationError``, for every settings class that reads the
environment: the assess vertical's and an LMS provider's (ADR-0033 D5).

One line per rejected variable, named by its environment alias, with pydantic's reason (the bound
or type it broke). The rejected VALUE is never echoed: settings carry DSNs and paths, and the
message goes to the startup log. The remediation names the variables to fix.
"""

from __future__ import annotations

from typing import Any

#: pydantic error types whose message names the rule (a bound, a type), never the input
SAFE_ERROR_TYPES = frozenset(
    {
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
        "int_parsing",
        "int_from_float",
        "float_parsing",
        "bool_parsing",
        "missing",
        "extra_forbidden",
        "literal_error",
        "string_too_short",
        "string_too_long",
        "finite_number",
    }
)


def explain_settings_error(
    exc: Exception, model: Any, *, fallback: str, docs: str
) -> tuple[str, str]:
    """``(detail, remediation)`` for ``exc`` raised by the settings class ``model``.

    ``fallback`` names the variables when none can be read from the error; ``docs`` is where the
    bounds are documented.
    """
    errors_of = getattr(exc, "errors", None)
    errors: list[Any] = list(errors_of()) if callable(errors_of) else []
    fields = getattr(model, "model_fields", {})
    by_name = {n: (f.alias or n) for n, f in fields.items()}
    names: list[str] = []
    lines: list[str] = []
    for err in errors:
        loc = err.get("loc") or ()
        key = str(loc[0]) if loc else ""
        var = by_name.get(key, key) or "settings"
        if var not in names:
            names.append(var)
        etype = str(err.get("type", ""))
        if etype in SAFE_ERROR_TYPES or (etype == "value_error" and not loc):
            # a bound or a type: pydantic's message names the rule, never the input. A
            # model-level validator's message is the class's own and names settings, not values
            msg = str(err.get("msg", "invalid value"))
        else:
            # a field validator's message may quote the rejected text
            msg = "invalid value"
        lines.append(f"{var}: {msg}")
    detail = "; ".join(lines) or type(exc).__name__
    remediation = f"Fix {', '.join(names) or fallback} (bounds in {docs})."
    return detail, remediation


__all__ = ["SAFE_ERROR_TYPES", "explain_settings_error"]

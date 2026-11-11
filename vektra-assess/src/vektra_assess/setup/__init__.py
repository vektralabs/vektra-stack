"""The REQ-079 setup check, generic part (ADR-0033 D3).

The runner, the report lifecycle and Vektra's own items live here; the service-account items and
the capability probes are the LMS provider's, reached through ``vektra_shared.lms.LmsProvider``.
"""

from vektra_assess.setup.items import (
    DEFAULT_REGISTRY,
    GENERIC_ITEMS,
    PLUGGABLE_ITEMS,
    builtin_registry,
    same_site,
)
from vektra_assess.setup.registry import (
    MISSING,
    OK,
    UNVERIFIABLE,
    CheckContext,
    ItemCheck,
    ItemRegistry,
    ItemResult,
    Outcome,
    SetupCheckStale,
    missing,
    ok,
    reason_code,
    unverifiable,
)
from vektra_assess.setup.runner import (
    TOKEN_REJECTED_EVENT,
    CheckOutcome,
    evaluate,
    persist_matrix,
    probe,
    run_items,
    run_setup_check,
)

__all__ = [
    "DEFAULT_REGISTRY",
    "GENERIC_ITEMS",
    "MISSING",
    "OK",
    "PLUGGABLE_ITEMS",
    "TOKEN_REJECTED_EVENT",
    "UNVERIFIABLE",
    "CheckContext",
    "CheckOutcome",
    "ItemCheck",
    "ItemRegistry",
    "ItemResult",
    "Outcome",
    "SetupCheckStale",
    "builtin_registry",
    "evaluate",
    "missing",
    "ok",
    "persist_matrix",
    "probe",
    "reason_code",
    "run_items",
    "run_setup_check",
    "same_site",
    "unverifiable",
]

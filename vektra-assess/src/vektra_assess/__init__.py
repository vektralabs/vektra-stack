"""Vektra assessment discussion chatbot (ADR-0027).

Sub-packages never import each other from their ``__init__``: a partial import graph stays
partial, and the import-linter contracts bind to real modules.
"""

from vektra_shared.log_safety import register_strict_logger_prefix

__version__ = "0.8.0"

# Every ``vektra_assess.*`` logger is held to the log allowlist (BR-011 / NFR-017). Registered
# here, at the package's first import, so no module of the package can log before it holds.
register_strict_logger_prefix("vektra_assess")

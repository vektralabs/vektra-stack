"""Vektra assessment discussion chatbot (ADR-0027).

Sub-packages never import each other from their ``__init__``: a partial import graph stays
partial, and the import-linter contracts bind to real modules. This module imports nothing at
all: the parser sidecar's forkserver preloads a module of this package, and every parsing child
inherits whatever this file loads (review of #203). The ``vektra_assess.*`` loggers are held to
the log allowlist by the composition roots that configure logging (``vektra_app.main`` and
``vektra_app.assess_worker`` call ``register_strict_logger_prefix(LOGGER_PREFIX)``).
"""

__version__ = "0.8.0"

#: the logger namespace of the vertical, registered as strict by the composition roots
LOGGER_PREFIX = "vektra_assess"

"""Parser sidecar: parses untrusted files and quiz review HTML (COMP-008, ARCH-066).

May import only ``vektra_assess.parser_protocol``, the stdlib, ``pdfminer``,
``defusedxml`` and ``html5lib`` (import-linter contract). Never imports the worker, the
database, the network stack or the ML stack.

Owning plan: .s2s/plans/20261101-154950-assess-parser.
"""

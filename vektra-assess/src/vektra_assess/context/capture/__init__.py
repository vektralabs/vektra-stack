"""Worker-only capture: attempt selection, disclosure projection, file intake, snapshot write.

Importable only from ``vektra_assess.worker`` and this package (import-linter contract). It is
the only part of ``vektra_assess.context`` that may touch the Moodle readers and the parser.
"""

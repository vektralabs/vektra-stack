"""Vektra assessment discussion chatbot (ADR-0027).

Sub-packages never import each other from their ``__init__``: a partial import graph stays
partial, and the import-linter contracts bind to real modules.
"""

__version__ = "0.8.0"

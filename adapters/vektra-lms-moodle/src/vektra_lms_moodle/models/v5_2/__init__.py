"""Moodle 5.2: strict payload models, one adapter per wsfunction."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from vektra_lms_moodle.models.v5_2 import assign, enrol, grading, quiz, site

ADAPTERS: dict[str, TypeAdapter[Any]] = {
    **assign.ADAPTERS,
    **enrol.ADAPTERS,
    **grading.ADAPTERS,
    **quiz.ADAPTERS,
    **site.ADAPTERS,
}

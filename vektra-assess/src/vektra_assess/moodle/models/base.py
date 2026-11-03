"""Shared base of the strict Moodle payload models (ARCH-070).

``extra="forbid"``, ``strict=True``, ``frozen=True``: an extra key, a missing required key or
a wrong type is a ``ValidationError``, which the readers map to ``Unreadable(unknown_shape)``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Warning(Strict):
    """One ``warnings[]`` entry. Any of these for a requested item makes a reader Unreadable."""

    item: str | None = None
    itemid: int | None = None
    warningcode: str
    message: str

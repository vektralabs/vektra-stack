"""The setup check's vocabulary (REQ-079, ADR-0033 D3): results, outcomes, the item registry.

Every item is ``OK``, ``MISSING`` or ``UNVERIFIABLE``; only ``OK`` counts (UNVERIFIABLE is not
OK) and every unknown fails closed. A report is content-free: ``{name, result, reason_code}``
per item, nothing the LMS said, no id.

The items here are Vektra's own (the UI origin, the parser, SMTP, the database log settings,
privacy, the provider registration). The service-account items are the LMS provider's: it
declares their names (``LmsProvider.setup_items``) and returns them from
``service_account_check``; the runner (``vektra_assess.setup.runner``) merges both.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from vektra_assess.config import VektraAssessSettings
from vektra_shared.lms import (
    SETUP_MISSING,
    SETUP_OK,
    SETUP_UNVERIFIABLE,
    LmsProvider,
    LmsSite,
    LmsSiteInfo,
    Reading,
    SetupItem,
)

OK = SETUP_OK
MISSING = SETUP_MISSING
UNVERIFIABLE = SETUP_UNVERIFIABLE

#: one item of a report; the provider builds the same type
ItemResult = SetupItem

NAME = re.compile(r"[a-z][a-z0-9_]{1,48}")
_CODE = re.compile(r"[a-z0-9_]{1,64}")


class SetupCheckStale(LookupError):
    """The site is unknown or the report is not runnable (finished, of another site, unknown):
    a stale job, completed without acting. Dedicated, so a ``KeyError``/``IndexError`` from a
    real bug is never mistaken for it. ``reason_code`` is what the job loop logs, once."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class Outcome:
    """What one item decided (the registry adds the name)."""

    result: str
    reason_code: str | None = None


def ok(reason: str | None = None) -> Outcome:
    return Outcome(OK, reason)


def missing(reason: str) -> Outcome:
    return Outcome(MISSING, reason)


def unverifiable(reason: str) -> Outcome:
    return Outcome(UNVERIFIABLE, reason)


def reason_code(value: str, default: str = "other") -> str:
    """A value as a reason code (``[a-z0-9_]{1,64}``), ``default`` when it is not one."""
    v = value.lower()
    return v if _CODE.fullmatch(v) else default


@dataclass(frozen=True)
class CheckContext:
    """What a Vektra-side item may look at. ``info`` is the provider's ``site_info``, read once
    for the whole run; ``site`` carries the release when it answered."""

    site: LmsSite
    settings: VektraAssessSettings
    manifest: Mapping[str, Any] | None
    info: Reading[LmsSiteInfo]
    # the site's ``moodle_origin`` column (revision 0033): the origin that frames the UI
    moodle_origin: str | None = None
    provider: LmsProvider | None = None


ItemCheck = Callable[[CheckContext], Awaitable[Outcome]]


class ItemRegistry:
    """Item name -> async check. Order of registration is the order of the report."""

    def __init__(self) -> None:
        self._items: dict[str, ItemCheck] = {}

    def register(self, name: str, check: ItemCheck) -> None:
        if not NAME.fullmatch(name):
            raise ValueError("item names are [a-z][a-z0-9_]{1,48}")
        if name in self._items:
            raise ValueError(f"item {name!r} is already registered")
        self._items[name] = check

    def get(self, name: str) -> ItemCheck | None:
        return self._items.get(name)

    def names(self) -> list[str]:
        return list(self._items)

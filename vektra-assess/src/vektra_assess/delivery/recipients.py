"""Transcript recipients (REQ-082 as amended 2026-11-07): the addresses the teacher configured.

The teacher enters 1..3 addresses in the plugin's activity form; they travel to Vektra inside the
SEALED teacher config (``transcript_recipients``), which only the worker can open (ARCH-071). This
module is the one place that reads them, for generation, for the delivery handler and for the
render worker's ``/state`` view:

- ``normalize_address``: the RFC 5321 subset Vektra accepts (one ``@``, a dot-atom local part, a
  lowercased LDH domain with at least one dot, no whitespace, no control character, no display
  name, at most 254 bytes). Anything else is dropped, never "repaired".
- ``configured_recipients``: the config's list, normalised, duplicates collapsed, at most
  ``MAX_RECIPIENTS`` (a 4th recipient never exists).
- ``recipient_ref``: ``sha256(site_id|address)`` hex. Delivery rows, logs and audit rows carry
  this ref only; the address is never stored outside the sealed config.
- ``mask``: ``m***@uni.example`` (first character of the local part), the only form ``/state``
  shows.
- ``domain_allowed``: exact match against ``VEKTRA_ASSESS_RECIPIENT_DOMAINS``; an empty list
  refuses every address.
- ``ConfigRecipients``: opens the activity's sealed ``transcript_recipients`` field (revision 0036:
  one sealed row per field, binding ``[site_id, cmid, "transcript_recipients"]``) and returns its
  recipients: ``()`` when the field is unset or cleared, ``None`` only when it does not open or
  is not a JSON list of strings (unreadable is never guessed).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from vektra_assess.crypto.teacher_config import (
    TRANSCRIPT_RECIPIENTS,
    read_teacher_config,
)
from vektra_assess.delivery.address import normalize_address
from vektra_assess.keys import AssessKeyring

MAX_RECIPIENTS = 3
ROLE_RECIPIENT = "recipient"
#: the sealed teacher-config field that lists the transcript recipients (plan 11's PUT writes it)
RECIPIENTS_KEY = TRANSCRIPT_RECIPIENTS


class GraderLike(Protocol):
    """A user holding the grading capability (``vektra_shared.lms.StaffMember`` fits; this
    package holds no LMS provider, import-linter contract (c)). Graders still decide who may
    download or resend (``api.graders``); they are no longer the recipients."""

    @property
    def user_id(self) -> int: ...

    @property
    def fullname(self) -> str: ...


def configured_recipients(
    config: Mapping[str, Any] | Sequence[Any] | str | None,
) -> tuple[str, ...]:
    """The configured addresses, normalised, unique, at most ``MAX_RECIPIENTS``. ``config`` is the
    opened field (a list of addresses), its JSON text, or a mapping holding it under
    ``transcript_recipients``."""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            return ()
    raw = config.get(RECIPIENTS_KEY) if isinstance(config, Mapping) else config
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[str] = []
    for item in raw:
        address = normalize_address(item)
        if address is not None and address not in out:
            out.append(address)
    return tuple(out[:MAX_RECIPIENTS])


def recipient_ref(site_id: object, address: str) -> str:
    return hashlib.sha256(f"{site_id}|{address}".encode()).hexdigest()


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower()


def parse_domains(value: str | Iterable[str] | None) -> frozenset[str]:
    """``VEKTRA_ASSESS_RECIPIENT_DOMAINS``: a comma list (or an iterable) of exact domains."""
    if value is None:
        return frozenset()
    parts = value.split(",") if isinstance(value, str) else list(value)
    return frozenset(p.strip().lower() for p in parts if p and p.strip())


def domain_allowed(address: str, domains: frozenset[str]) -> bool:
    """Exact domain match; an empty allowlist refuses everything (REQ-082 amendment)."""
    return bool(domains) and domain_of(address) in domains


def mask(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}"


def state_view(addresses: Sequence[str]) -> list[dict[str, str]]:
    """The ``/state`` ``pre_session.recipients`` items: role and masked address, nothing else."""
    return [{"role": ROLE_RECIPIENT, "name": mask(a)} for a in addresses]


class Conn(Protocol):
    async def execute(self, statement: Any, parameters: Any = ..., /) -> Any: ...


class ConfigRecipients:
    """Worker side: reads the activity's sealed recipients with the worker's private key."""

    def __init__(self, private: AssessKeyring) -> None:
        self._private = private

    async def read(
        self, conn: Conn, site_id: object, cmid: int
    ) -> tuple[str, ...] | None:
        """The configured recipients. ``None`` only when unreadable (a seal that does not open,
        not a JSON list of strings); ``()`` when readable and nobody is configured (never set, or
        cleared by the teacher)."""
        opened = await read_teacher_config(
            conn, self._private, site_id, int(cmid), (TRANSCRIPT_RECIPIENTS,)
        )
        if TRANSCRIPT_RECIPIENTS in opened.unreadable:
            return None
        return configured_recipients(list(opened.transcript_recipients or ()))

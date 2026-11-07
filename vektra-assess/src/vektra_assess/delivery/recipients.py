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
- ``mask``: ``m***@unical.it`` (first character of the local part), the only form ``/state``
  shows.
- ``domain_allowed``: exact match against ``VEKTRA_ASSESS_RECIPIENT_DOMAINS``; an empty list
  refuses every address.
- ``ConfigRecipients``: opens the sealed config of one activity (binding field ``config``) and
  returns its recipients, or ``None`` when there is no config, it does not open, or it is not a
  JSON object (unreadable is never guessed).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from sqlalchemy import text

from vektra_assess.crypto.teacher_config import open_teacher_config
from vektra_assess.keys import AssessKeyring

MAX_RECIPIENTS = 3
MAX_ADDRESS_BYTES = 254
ROLE_RECIPIENT = "recipient"
#: the key of the sealed teacher config that lists the transcript recipients (plan 11 writes it)
RECIPIENTS_KEY = "transcript_recipients"
#: binding field of the whole sealed teacher config (the rotation job re-seals under it too)
CONFIG_FIELD = "config"

_LOCAL = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$"
)
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")


class GraderLike(Protocol):
    """A user holding the grading capability (``moodle.domain.GraderView`` fits; this package
    must not import ``vektra_assess.moodle``, import-linter contract (c)). Graders still decide
    who may download or resend (``api.graders``); they are no longer the recipients."""

    @property
    def userid(self) -> int: ...

    @property
    def fullname(self) -> str: ...


def normalize_address(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or len(value.encode("utf-8")) > MAX_ADDRESS_BYTES:
        return None
    if value.count("@") != 1:
        return None
    local, domain = value.split("@")
    domain = domain.lower()
    if len(local) > 64 or not _LOCAL.match(local) or not _DOMAIN.match(domain):
        return None
    return f"{local}@{domain}"


def configured_recipients(config: Mapping[str, Any] | str | None) -> tuple[str, ...]:
    """The configured addresses, normalised, unique, at most ``MAX_RECIPIENTS``."""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            return ()
    if not isinstance(config, Mapping):
        return ()
    raw = config.get(RECIPIENTS_KEY)
    if not isinstance(raw, list):
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


_SEALED = text(
    "SELECT teacher_config_sealed FROM assess_activity_config "
    "WHERE site_id = :s AND cmid = :c"
)


class ConfigRecipients:
    """Worker side: reads the activity's sealed teacher config with the worker's private key."""

    def __init__(self, private: AssessKeyring) -> None:
        self._private = private

    async def read(
        self, conn: Conn, site_id: object, cmid: int
    ) -> tuple[str, ...] | None:
        """The configured recipients; ``None`` when unreadable (no config, a seal that does not
        open, not a JSON object). ``()``: readable, nobody configured."""
        blob = (await conn.execute(_SEALED, {"s": site_id, "c": int(cmid)})).scalar()
        if blob is None:
            return None
        try:
            plain = open_teacher_config(
                self._private, bytes(blob), site_id, int(cmid), CONFIG_FIELD
            )
            doc = json.loads(plain)
        except Exception:  # InvalidTag, unknown kid, not UTF-8, not JSON: unreadable
            return None
        if not isinstance(doc, dict):
            return None
        return configured_recipients(doc)

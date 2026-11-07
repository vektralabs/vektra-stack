"""The e-mail address subset Vektra accepts (RFC 5321 addr-spec, no display name).

One rule for the configured transcript recipients (REQ-082 as amended) and for the From address
of the worker (``VEKTRA_ASSESS_SMTP_FROM``): one ``@``, a dot-atom local part, a lowercased LDH
domain with at least one dot, no whitespace, no control character, no ``<>`` or display name, at
most 254 bytes. Anything else is refused, never "repaired". Pure: imported by the settings class.
"""

from __future__ import annotations

import re

MAX_ADDRESS_BYTES = 254

_LOCAL = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$"
)
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")


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

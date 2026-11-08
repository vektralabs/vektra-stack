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
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate: not text an SMTP server can carry
        return None
    if not value or size > MAX_ADDRESS_BYTES:
        return None
    if value.count("@") != 1:
        return None
    local, domain = value.split("@")
    domain = domain.lower()
    # fullmatch: ``$`` alone also matches before a trailing newline
    if len(local) > 64 or not _LOCAL.fullmatch(local) or not _DOMAIN.fullmatch(domain):
        return None
    return f"{local}@{domain}"

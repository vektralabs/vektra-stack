"""The e-mail address subset Vektra accepts (RFC 5321 addr-spec, no display name).

One rule for the configured transcript recipients (REQ-082 as amended) and for the From address
of the worker (``VEKTRA_ASSESS_SMTP_FROM``): one ``@``, a dot-atom local part, a lowercased LDH
domain with at least one dot, no whitespace, no control character, no ``<>`` or display name, at
most 254 bytes. Anything else is refused, never "repaired". The rule itself lives in
``vektra_assess.config`` (the settings validate the From with it, and the parser sidecar, which
loads the settings, may not import this package).
"""

from __future__ import annotations

from vektra_assess.config import MAX_ADDRESS_BYTES, normalize_address

__all__ = ["MAX_ADDRESS_BYTES", "normalize_address"]

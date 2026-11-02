"""KEK wrapping of per-session DEKs (plan assess-data-crypto 4.2).

The KEK keyring (``assess_kek``) is held by the worker only. A wrapped DEK is bound to
``(session_id, key_version)``. An envelope naming a kid that is not in the keyring raises
``KeyNotAvailable``: there is never a fallback to another kid.
"""

from __future__ import annotations

from vektra_assess.crypto.envelope import Purpose, aead_decrypt, aead_encrypt, peek_kid
from vektra_assess.keys import AssessKeyring


class KeyNotAvailable(LookupError):
    """The key id an envelope names is not in the loaded keyring."""


def key_for(keyring: AssessKeyring, kid: str) -> bytes:
    try:
        return keyring.keys[kid]
    except KeyError:
        raise KeyNotAvailable(f"{keyring.name}: kid {kid!r} is not available") from None


def wrap_dek(
    keyring: AssessKeyring, dek: bytes, session_id: object, key_version: int = 1
) -> tuple[bytes, str, int]:
    """Wrap ``dek`` under the keyring's active key. Returns ``(blob, kid, key_version)``."""
    blob = aead_encrypt(
        keyring.active_key,
        keyring.active,
        dek,
        purpose=Purpose.DEK_WRAP,
        binding=[session_id, key_version],
    )
    return blob, keyring.active, key_version


def unwrap_dek(
    keyring: AssessKeyring, blob: bytes, session_id: object, key_version: int = 1
) -> bytes:
    key = key_for(keyring, peek_kid(blob))
    return aead_decrypt(
        key, blob, purpose=Purpose.DEK_WRAP, binding=[session_id, key_version]
    )

"""The WS token of a site, read from the worker's ``assess_ws_tokens`` secret at call time.

Never from the environment or the database (ARCH-066). The secret is a credentials file
``{"<site_id>": "<token>", ...}``; it is re-read on every call so a rotation needs no
restart, and a missing or malformed file yields ``None`` (the caller reports Unreadable,
it never raises with the token in a message).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from vektra_assess.keys import Credentials, KeyringError, load_keyring

SECRET_NAME = "assess_ws_tokens"


def secret_token_provider(secrets_dir: Path) -> Callable[[str], str | None]:
    def provider(site_id: str) -> str | None:
        try:
            loaded = load_keyring(SECRET_NAME, secrets_dir)
        except KeyringError:
            return None
        if not isinstance(loaded, Credentials):
            return None
        token = loaded.data.get(site_id)
        return token if isinstance(token, str) and token else None

    return provider

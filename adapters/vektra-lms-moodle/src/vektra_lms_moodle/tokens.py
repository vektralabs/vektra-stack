"""The WS token of a site, read from the worker's ``assess_ws_tokens`` secret at call time.

Never from the environment or the database (ARCH-066). The secret is a credentials file
``{"<site_id>": "<token>", ...}``; it is re-read on every call so a rotation needs no
restart, and a missing or malformed file yields ``None`` (the caller reports Unreadable,
it never raises with the token in a message). The name is this provider's own (ADR-0033 D5):
it declares it as ``required_secrets``, the worker's key matrix checks and loads it at startup,
and this reader applies the same file check (``vektra_shared.secret_files``) on every read.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from vektra_shared.secret_files import SecretFileError, load_json_object

SECRET_NAME = "assess_ws_tokens"


def secret_token_provider(secrets_dir: Path) -> Callable[[str], str | None]:
    def provider(site_id: str) -> str | None:
        try:
            data = load_json_object(secrets_dir / SECRET_NAME)
        except (SecretFileError, OSError):
            return None
        token = data.get(site_id)
        return token if isinstance(token, str) and token else None

    return provider

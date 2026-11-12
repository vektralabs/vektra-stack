"""Docker-secret files: the one file check, shared by the assess key matrix and LMS providers.

A secret is a regular file (never a symlink), owned by this process or by root, readable by at most
owner and group (``MAX_MODE``). ``vektra_assess.keys`` applies it to every assess keyring, and an
LMS provider applies it to the secrets it declares (``LmsProvider.required_secrets``, ADR-0033 D5)
when it re-reads them at call time, so both read a file the same way the worker's startup checked
it. Messages name the file, never its content.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_MODE = 0o440  # owner and group read, nothing else


class SecretFileError(ValueError):
    """A secret file is missing, unsafe or malformed. The message never contains its content."""


def check_secret_file(path: Path) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        raise SecretFileError(f"{path.name}: file not found") from None
    if not stat.S_ISREG(st.st_mode):
        raise SecretFileError(f"{path.name}: not a regular file (symlinks are refused)")
    if st.st_uid not in (os.getuid(), 0):
        raise SecretFileError(
            f"{path.name}: owned by uid {st.st_uid}, not by this process or root"
        )
    mode = stat.S_IMODE(st.st_mode)
    if mode & ~MAX_MODE:
        raise SecretFileError(
            f"{path.name}: mode {mode:04o} is too open (at most {MAX_MODE:04o})"
        )


def load_json_object(path: Path) -> Mapping[str, Any]:
    """A checked secret file holding one JSON object (a credentials secret)."""
    check_secret_file(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SecretFileError(f"{path.name}: not valid JSON") from None
    if not isinstance(doc, dict):
        raise SecretFileError(f"{path.name}: expected a JSON object")
    return doc


__all__ = ["MAX_MODE", "SecretFileError", "check_secret_file", "load_json_object"]

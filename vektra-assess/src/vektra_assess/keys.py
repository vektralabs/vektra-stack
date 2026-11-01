"""Keyring loading and the key matrix (ARCH-066, ARCH-071).

Each assess secret is a Docker secret file under ``/run/secrets/`` holding a JSON
keyring, ``{"active": "<kid>", "keys": {"<kid>": "<base64>"}}``, so rotation is a file
change (add a kid, flip ``active``) and not a schema change. Credential secrets (WS
tokens, SMTP) hold a JSON object instead.

``KEY_MATRIX`` says, for every secret and every process role, whether the process MUST
hold it, MUST NOT hold it, or may. ``enforce_key_matrix(role)`` applies it in both
directions at process start: a missing required key and a present forbidden key are
both refusals, and a forbidden key counts as present when its file exists OR when any
environment variable carries its name (defence against a leaked ``.env``). The API
applies the forbidden half even when assess is disabled: an API container that happens
to hold the WS tokens is the dormant capability ARCH-066 forbids.

Key bytes are never logged and never appear in a ``repr``.

Usage: ``python -m vektra_assess.keys generate --out <dir>`` writes a complete test set
(mode 0400). Owning plan: .s2s/plans/20261101-154950-assess-foundations.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import secrets
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from vektra_shared.startup import StartupValidationError

Role = Literal["api", "worker", "parser"]
ROLES: tuple[Role, ...] = ("api", "worker", "parser")


class Need(StrEnum):
    REQUIRED = "required"
    FORBIDDEN = "forbidden"
    ALLOWED = "allowed"


class Kind(StrEnum):
    SYMMETRIC = "symmetric"  # 32 random bytes (AES-256 KEK / HMAC signing key)
    X25519_PRIVATE = "x25519_private"  # 32-byte raw private key
    X25519_PUBLIC = "x25519_public"  # 32-byte raw public key
    CREDENTIALS = "credentials"  # JSON object (WS tokens per site, SMTP account)
    PASSWORD = "password"  # one line of text: a Postgres role password


@dataclass(frozen=True)
class KeySpec:
    kind: Kind
    api: Need
    worker: Need
    parser: Need

    def need(self, role: Role) -> Need:
        need: Need = getattr(self, role)
        return need


R, F, A = Need.REQUIRED, Need.FORBIDDEN, Need.ALLOWED

# ARCH-071. The parser holds nothing: it parses untrusted files with no network.
KEY_MATRIX: Mapping[str, KeySpec] = {
    "assess_token_signing": KeySpec(Kind.SYMMETRIC, api=R, worker=F, parser=F),
    "assess_handoff_private": KeySpec(Kind.X25519_PRIVATE, api=R, worker=F, parser=F),
    "assess_handoff_public": KeySpec(Kind.X25519_PUBLIC, api=A, worker=R, parser=F),
    "assess_transcript_kek": KeySpec(Kind.SYMMETRIC, api=R, worker=R, parser=F),
    "assess_teacher_config_public": KeySpec(
        Kind.X25519_PUBLIC, api=R, worker=A, parser=F
    ),
    "assess_teacher_config_private": KeySpec(
        Kind.X25519_PRIVATE, api=F, worker=R, parser=F
    ),
    "assess_kek": KeySpec(Kind.SYMMETRIC, api=F, worker=R, parser=F),
    "assess_ws_tokens": KeySpec(Kind.CREDENTIALS, api=F, worker=R, parser=F),
    "assess_smtp": KeySpec(Kind.CREDENTIALS, api=F, worker=R, parser=F),
    # Role passwords: never in a DSN, never in .env. Each process holds only its own;
    # the one-shot vektra-assess-db-init holds both (DB_INIT_SECRETS).
    "assess_db_api_password": KeySpec(Kind.PASSWORD, api=R, worker=F, parser=F),
    "assess_db_worker_password": KeySpec(Kind.PASSWORD, api=F, worker=R, parser=F),
}

# The only secrets the one-shot role-provisioning service may hold (compose policy test).
DB_INIT_SECRETS: frozenset[str] = frozenset(
    {"assess_db_api_password", "assess_db_worker_password"}
)
MIN_PASSWORD_CHARS = 24

KEY_BYTES = 32
MAX_MODE = 0o440  # owner and group read, nothing else
# Env var fragments that make a parser environment unsafe (DSNs, key material).
_PARSER_FORBIDDEN_ENV_FRAGMENTS = ("DATABASE_URL", "DSN", "PASSWORD", "SECRET", "TOKEN")


class KeyringError(ValueError):
    """A keyring file is malformed or unsafe. The message never contains key bytes."""


@dataclass(frozen=True)
class AssessKeyring:
    """A loaded keyring. ``keys`` maps kid to raw bytes; repr is redacted."""

    name: str
    active: str
    keys: Mapping[str, bytes] = field(repr=False)

    @property
    def active_key(self) -> bytes:
        return self.keys[self.active]

    def __repr__(self) -> str:
        return f"AssessKeyring(name={self.name!r}, active={self.active!r}, kids={sorted(self.keys)!r})"


@dataclass(frozen=True)
class Credentials:
    """A loaded credentials secret. Values are never shown in repr."""

    name: str
    data: Mapping[str, Any] = field(repr=False)

    def __repr__(self) -> str:
        return f"Credentials(name={self.name!r}, fields={sorted(self.data)!r})"


@dataclass(frozen=True)
class Password:
    """A loaded role password. Never shown in repr."""

    name: str
    value: str = field(repr=False)

    def __repr__(self) -> str:
        return f"Password(name={self.name!r})"


Loaded = AssessKeyring | Credentials | Password


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _check_file(path: Path) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        raise KeyringError(f"{path.name}: file not found") from None
    if not stat.S_ISREG(st.st_mode):
        raise KeyringError(f"{path.name}: not a regular file (symlinks are refused)")
    if st.st_uid not in (os.getuid(), 0):
        raise KeyringError(
            f"{path.name}: owned by uid {st.st_uid}, not by this process or root"
        )
    mode = stat.S_IMODE(st.st_mode)
    if mode & ~MAX_MODE:
        raise KeyringError(
            f"{path.name}: mode {mode:04o} is too open (at most {MAX_MODE:04o})"
        )


def load_keyring(name: str, secrets_dir: Path) -> Loaded:
    """Load and validate the secret ``name`` from ``secrets_dir``."""
    if name not in KEY_MATRIX:
        raise KeyringError(f"{name}: not an assess secret")
    spec = KEY_MATRIX[name]
    path = secrets_dir / name
    _check_file(path)
    if spec.kind is Kind.PASSWORD:
        try:
            value = path.read_text(encoding="utf-8").rstrip("\n")
        except UnicodeDecodeError:
            raise KeyringError(f"{name}: not valid UTF-8") from None
        if len(value) < MIN_PASSWORD_CHARS or any(c.isspace() for c in value):
            raise KeyringError(
                f"{name}: must be one line of at least {MIN_PASSWORD_CHARS} "
                "non-whitespace characters"
            )
        return Password(name=name, value=value)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise KeyringError(f"{name}: not valid JSON") from None
    if not isinstance(doc, dict):
        raise KeyringError(f"{name}: expected a JSON object")
    if spec.kind is Kind.CREDENTIALS:
        return Credentials(name=name, data=doc)

    active, raw_keys = doc.get("active"), doc.get("keys")
    if not isinstance(active, str) or not isinstance(raw_keys, dict) or not raw_keys:
        raise KeyringError(
            f"{name}: expected {{'active': kid, 'keys': {{kid: base64}}}}"
        )
    if active not in raw_keys:
        raise KeyringError(f"{name}: active kid {active!r} is not in keys")
    keys: dict[str, bytes] = {}
    for kid, b64 in raw_keys.items():
        if not isinstance(b64, str):
            raise KeyringError(f"{name}: kid {kid!r} is not a base64 string")
        try:
            key = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise KeyringError(f"{name}: kid {kid!r} is not valid base64") from None
        if len(key) != KEY_BYTES:
            raise KeyringError(
                f"{name}: kid {kid!r} is {len(key)} bytes, expected {KEY_BYTES}"
            )
        keys[kid] = key
    return AssessKeyring(name=name, active=active, keys=keys)


# ---------------------------------------------------------------------------
# Matrix enforcement
# ---------------------------------------------------------------------------


def _env_names_carrying(name: str, environ: Mapping[str, str]) -> list[str]:
    needle = name.upper()
    return sorted(k for k in environ if needle in k.upper())


def enforce_key_matrix(
    role: Role,
    *,
    secrets_dir: Path,
    environ: Mapping[str, str] | None = None,
    forbidden_only: bool = False,
    step: str = "assess_check",
) -> dict[str, Loaded]:
    """Apply ``KEY_MATRIX`` for ``role``; return the loaded required/allowed secrets.

    Raises ``StartupValidationError(step=step)`` naming every violated key. With
    ``forbidden_only`` only the "must not hold" half runs (the API when assess is off).
    """
    env = os.environ if environ is None else environ
    problems: list[str] = []
    loaded: dict[str, Loaded] = {}

    for name, spec in KEY_MATRIX.items():
        need = spec.need(role)
        present = (secrets_dir / name).exists() or (secrets_dir / name).is_symlink()
        if need is Need.FORBIDDEN:
            if present:
                problems.append(
                    f"{name}: forbidden for {role} but mounted at {secrets_dir / name}"
                )
            carriers = _env_names_carrying(name, env)
            if carriers:
                problems.append(
                    f"{name}: forbidden for {role} but carried by env var(s) {carriers}"
                )
            continue
        if forbidden_only:
            continue
        if not present:
            if need is Need.REQUIRED:
                problems.append(
                    f"{name}: required for {role} but missing in {secrets_dir}"
                )
            continue
        try:
            loaded[name] = load_keyring(name, secrets_dir)
        except KeyringError as exc:
            problems.append(str(exc))

    if role == "parser":
        problems.extend(_parser_environment_problems(secrets_dir, env))

    if problems:
        raise StartupValidationError(
            step=step,
            detail="; ".join(problems),
            remediation=(
                f"Mount exactly the secrets the {role} role needs (KEY_MATRIX in "
                "vektra_assess/keys.py, docs/reference/configuration.md): required "
                "keys present, mode <= 0440, forbidden keys neither mounted nor in env."
            ),
        )
    return loaded


def _parser_environment_problems(
    secrets_dir: Path, env: Mapping[str, str]
) -> list[str]:
    problems: list[str] = []
    if secrets_dir.exists() and any(secrets_dir.iterdir()):
        problems.append(f"parser: secrets dir {secrets_dir} must be absent or empty")
    for k in sorted(env):
        up = k.upper()
        if up.startswith("VEKTRA_") and any(
            f in up for f in _PARSER_FORBIDDEN_ENV_FRAGMENTS
        ):
            problems.append(f"parser: env var {k} must not be set (no DSN or secret)")
    return problems


# ---------------------------------------------------------------------------
# Generator (test sets and first install)
# ---------------------------------------------------------------------------


def _keyring_doc(kid: str, key: bytes) -> dict[str, Any]:
    return {"active": kid, "keys": {kid: base64.b64encode(key).decode("ascii")}}


def generate(out: Path, *, kid: str = "k1") -> list[Path]:
    """Write every secret of ``KEY_MATRIX`` into ``out`` with mode 0400.

    X25519 secrets are generated as matching pairs. Credentials are written as empty
    JSON objects for the operator to fill. Existing files are never overwritten.
    """
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    out.mkdir(parents=True, exist_ok=True)
    docs: dict[str, dict[str, Any]] = {}
    for base in ("assess_handoff", "assess_teacher_config"):
        priv = X25519PrivateKey.generate()
        raw_priv = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        raw_pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        docs[f"{base}_private"] = _keyring_doc(kid, raw_priv)
        docs[f"{base}_public"] = _keyring_doc(kid, raw_pub)
    for name, spec in KEY_MATRIX.items():
        if spec.kind is Kind.SYMMETRIC:
            docs[name] = _keyring_doc(kid, secrets.token_bytes(KEY_BYTES))
        elif spec.kind is Kind.CREDENTIALS:
            docs[name] = {}
    passwords = {
        n: secrets.token_urlsafe(32)
        for n, sp in KEY_MATRIX.items()
        if sp.kind is Kind.PASSWORD
    }
    missing = set(KEY_MATRIX) - set(docs) - set(passwords)
    assert not missing, missing

    written: list[Path] = []
    for name in KEY_MATRIX:
        path = out / name
        if path.exists():
            raise FileExistsError(f"{path} exists; refusing to overwrite a secret")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if name in passwords:
                fh.write(passwords[name] + "\n")
            else:
                json.dump(docs[name], fh)
        os.chmod(path, 0o400)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vektra_assess.keys")
    sub = parser.add_subparsers(dest="cmd", required=True)
    gen = sub.add_parser("generate", help="write a complete set of assess keyrings")
    gen.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "generate":
        for path in generate(args.out):
            print(path)  # names only, never contents
    return 0


if __name__ == "__main__":
    sys.exit(main())

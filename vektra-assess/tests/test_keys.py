"""Key matrix enforcement in both directions (plan assess-foundations 5.4, ARCH-071).

Every refusal is asserted to NAME the key that caused it: a check that refuses for the
wrong reason would pass these tests otherwise.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from vektra_assess import keys
from vektra_assess.keys import KEY_MATRIX, ROLES, Need, enforce_key_matrix, generate
from vektra_shared.startup import StartupValidationError

FULL_ENV: dict[str, str] = {}


@pytest.fixture
def full_set(tmp_path: Path) -> Path:
    out = tmp_path / "all"
    generate(out)
    return out


def _subset(src: Path, dst: Path, names: set[str]) -> Path:
    dst.mkdir()
    for n in names:
        target = dst / n
        target.write_bytes((src / n).read_bytes())
        os.chmod(target, 0o400)
    return dst


def _role_set(full: Path, tmp: Path, role: str, extra: set[str] = frozenset()) -> Path:  # type: ignore[assignment]
    names = {n for n, s in KEY_MATRIX.items() if s.need(role) is Need.REQUIRED}  # type: ignore[arg-type]
    return _subset(
        full, tmp / f"{role}-{len(extra)}-{'-'.join(sorted(extra))}", names | extra
    )


def _refusal(role: str, d: Path, env: dict[str, str] | None = None, **kw) -> str:  # type: ignore[no-untyped-def]
    with pytest.raises(StartupValidationError) as exc:
        enforce_key_matrix(role, secrets_dir=d, environ=env or {}, **kw)  # type: ignore[arg-type]
    return exc.value.detail


def test_matrix_covers_three_roles_and_parser_holds_nothing() -> None:
    for name, spec in KEY_MATRIX.items():
        assert spec.parser is Need.FORBIDDEN, name
    assert {"api", "worker", "parser"} == set(ROLES)
    # ARCH-066: the API never holds the KEK, WS tokens or SMTP
    for name in (
        "assess_kek",
        "assess_ws_tokens",
        "assess_smtp",
        "assess_teacher_config_private",
    ):
        assert KEY_MATRIX[name].api is Need.FORBIDDEN


@pytest.mark.parametrize("role", ["api", "worker"])
def test_generated_set_minus_forbidden_satisfies_role(
    full_set: Path, tmp_path: Path, role: str
) -> None:
    d = _role_set(full_set, tmp_path, role)
    loaded = enforce_key_matrix(role, secrets_dir=d, environ={})  # type: ignore[arg-type]
    required = {n for n, s in KEY_MATRIX.items() if s.need(role) is Need.REQUIRED}  # type: ignore[arg-type]
    assert required <= set(loaded)


def test_parser_with_no_secrets_dir_passes(tmp_path: Path) -> None:
    assert (
        enforce_key_matrix("parser", secrets_dir=tmp_path / "absent", environ={}) == {}
    )


@pytest.mark.parametrize(
    ("role", "name"),
    [
        (r, n)
        for r in ("api", "worker")
        for n, s in KEY_MATRIX.items()
        if s.need(r) is Need.REQUIRED
    ],  # type: ignore[arg-type]
)
def test_missing_required_key_refused_by_name(
    full_set: Path, tmp_path: Path, role: str, name: str
) -> None:
    required = {n for n, s in KEY_MATRIX.items() if s.need(role) is Need.REQUIRED}  # type: ignore[arg-type]
    d = _subset(full_set, tmp_path / "d", required - {name})
    detail = _refusal(role, d)
    assert f"{name}: required for {role}" in detail
    assert detail.count("required for") == 1


@pytest.mark.parametrize(
    ("role", "name"),
    [
        (r, n)
        for r in ROLES
        for n, s in KEY_MATRIX.items()
        if s.need(r) is Need.FORBIDDEN
    ],
)
def test_forbidden_key_file_refused_by_name(
    full_set: Path, tmp_path: Path, role: str, name: str
) -> None:
    if role == "parser":
        d = _subset(full_set, tmp_path / "p", {name})
    else:
        d = _role_set(full_set, tmp_path, role, {name})
    detail = _refusal(role, d)
    assert f"{name}: forbidden for {role} but mounted" in detail


@pytest.mark.parametrize(
    ("role", "name"),
    [
        (r, n)
        for r in ROLES
        for n, s in KEY_MATRIX.items()
        if s.need(r) is Need.FORBIDDEN
    ],
)
def test_forbidden_key_env_var_refused_by_name(
    full_set: Path, tmp_path: Path, role: str, name: str
) -> None:
    d = tmp_path / "absent" if role == "parser" else _role_set(full_set, tmp_path, role)
    env = {f"{name.upper()}_FILE": "/somewhere"}
    detail = _refusal(role, d, env)
    assert f"{name}: forbidden for {role} but carried by env var" in detail


def test_api_forbidden_half_runs_even_when_only_forbidden_checked(
    full_set: Path, tmp_path: Path
) -> None:
    """Assess disabled: the API holds no required keys, but a WS keyring still refuses."""
    d = _subset(full_set, tmp_path / "ws", {"assess_ws_tokens"})
    detail = _refusal("api", d, forbidden_only=True)
    assert "assess_ws_tokens: forbidden for api" in detail
    assert "required" not in detail
    # and an empty dir passes the forbidden-only half
    assert (
        enforce_key_matrix(
            "api", secrets_dir=tmp_path / "none", environ={}, forbidden_only=True
        )
        == {}
    )


def test_mode_0644_refused(full_set: Path, tmp_path: Path) -> None:
    d = _role_set(full_set, tmp_path, "worker")
    os.chmod(d / "assess_kek", 0o644)
    assert "assess_kek: mode 0644 is too open" in _refusal("worker", d)


def test_wrong_key_length_refused(full_set: Path, tmp_path: Path) -> None:
    d = _role_set(full_set, tmp_path, "worker")
    p = d / "assess_kek"
    os.chmod(p, 0o600)
    p.write_text(
        json.dumps(
            {"active": "k1", "keys": {"k1": base64.b64encode(b"x" * 16).decode()}}
        )
    )
    os.chmod(p, 0o400)
    assert "assess_kek: kid 'k1' is 16 bytes, expected 32" in _refusal("worker", d)


def test_symlink_refused(full_set: Path, tmp_path: Path) -> None:
    d = _role_set(full_set, tmp_path, "worker")
    (d / "assess_kek").unlink()
    (d / "assess_kek").symlink_to(full_set / "assess_kek")
    assert "assess_kek: not a regular file" in _refusal("worker", d)


def test_malformed_json_and_active_kid(full_set: Path, tmp_path: Path) -> None:
    d = _role_set(full_set, tmp_path, "worker")
    p = d / "assess_kek"
    os.chmod(p, 0o600)
    p.write_text("not json")
    os.chmod(p, 0o400)
    assert "assess_kek: not valid JSON" in _refusal("worker", d)
    os.chmod(p, 0o600)
    p.write_text(
        json.dumps(
            {"active": "k2", "keys": {"k1": base64.b64encode(b"x" * 32).decode()}}
        )
    )
    os.chmod(p, 0o400)
    assert "active kid 'k2' is not in keys" in _refusal("worker", d)


def test_parser_refuses_dsn_env_and_nonempty_secrets_dir(tmp_path: Path) -> None:
    detail = _refusal(
        "parser", tmp_path / "absent", {"VEKTRA_DATABASE_URL": "postgresql://x"}
    )
    assert "VEKTRA_DATABASE_URL must not be set" in detail
    d = tmp_path / "s"
    d.mkdir()
    (d / "anything").write_text("x")
    assert "must be absent or empty" in _refusal("parser", d)


def test_every_violation_is_listed(full_set: Path, tmp_path: Path) -> None:
    d = _subset(full_set, tmp_path / "bad", {"assess_kek", "assess_smtp"})
    detail = _refusal("api", d)
    for name in (
        "assess_kek",
        "assess_smtp",
        "assess_token_signing",
        "assess_handoff_private",
    ):
        assert name in detail


def test_repr_and_errors_never_show_key_bytes(full_set: Path, tmp_path: Path) -> None:
    d = _role_set(full_set, tmp_path, "worker")
    loaded = enforce_key_matrix("worker", secrets_dir=d, environ={})
    raw = json.loads((full_set / "assess_kek").read_text())["keys"]["k1"]
    assert raw not in repr(loaded) and raw not in str(loaded)
    assert loaded["assess_kek"].active_key == base64.b64decode(raw)  # type: ignore[union-attr]


def test_generated_files_are_0400_and_pairs_match(full_set: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    for name in KEY_MATRIX:
        assert (full_set / name).stat().st_mode & 0o777 == 0o400, name
    for base in ("assess_handoff", "assess_teacher_config"):
        priv = keys.load_keyring(f"{base}_private", full_set)
        pub = keys.load_keyring(f"{base}_public", full_set)
        derived = X25519PrivateKey.from_private_bytes(priv.active_key).public_key()  # type: ignore[union-attr]
        assert derived.public_bytes(Encoding.Raw, PublicFormat.Raw) == pub.active_key  # type: ignore[union-attr]


def test_generate_refuses_to_overwrite(full_set: Path) -> None:
    with pytest.raises(FileExistsError):
        generate(full_set)


def test_cli_prints_names_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert keys.main(["generate", "--out", str(tmp_path / "cli")]) == 0
    out = capsys.readouterr().out
    assert "assess_kek" in out and "keys" not in out.replace("assess_", "")

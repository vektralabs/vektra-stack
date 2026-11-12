"""Second review of #205 (findings 1 and 6): every public setup entry point fails closed on an
unwrapped provider, and an intact snapshot of an old schema is not reported as tampering."""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace
from typing import Any

from support.fake_lms import SITE, FakeLmsProvider

from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.capture.store import (
    Mismatch,
    MismatchReason,
    verify_snapshot_integrity,
)
from vektra_assess.context.snapshot import (
    SCHEMA_VERSION,
    UnsupportedSchemaError,
    from_canonical_bytes,
)
from vektra_assess.crypto import envelope
from vektra_assess.setup import UNVERIFIABLE, CheckContext, run_items
from vektra_shared.lms import Readable, guarded


class _Raising(FakeLmsProvider):
    async def service_account_check(self, *_a: Any, **_k: Any) -> Any:
        raise RuntimeError("boom")


async def test_run_items_wraps_an_unwrapped_provider() -> None:
    """Finding 1: ``run_items`` is public; a raising provider handed to it directly (not
    through ``evaluate``) is still every declared item UNVERIFIABLE/check_error."""
    settings = VektraAssessSettings(VEKTRA_ASSESS_ENABLED="true")  # type: ignore[call-arg]
    provider = _Raising(setup_items=("token_works", "required_reads"))
    ctx = CheckContext(SITE, settings, None, Readable(None), None, provider)  # type: ignore[arg-type]
    assert ctx.provider is not provider  # wrapped once, here
    assert guarded(ctx.provider) is ctx.provider  # type: ignore[arg-type]
    items = {i.name: (i.result, i.reason_code) for i in await run_items(ctx)}
    assert items["token_works"] == (UNVERIFIABLE, "check_error")
    assert items["required_reads"] == (UNVERIFIABLE, "check_error")


def _v1_bytes() -> bytes:
    return json.dumps({"header": {"schema_version": SCHEMA_VERSION - 1}}).encode()


def test_an_old_schema_is_its_own_error_not_a_parse_failure() -> None:
    try:
        from_canonical_bytes(_v1_bytes())
    except UnsupportedSchemaError as exc:
        assert exc.version == SCHEMA_VERSION - 1
    else:  # pragma: no cover
        raise AssertionError("an old schema must not parse")


async def test_an_intact_old_snapshot_is_audited_as_unsupported_not_tampered() -> None:
    """Finding 6: the AEAD tag holds and only the format is old: ``unsupported_schema``, not
    ``unparseable`` (which reads as damage)."""
    sid, dek = uuid.uuid4(), os.urandom(32)
    blob = envelope.encrypt_snapshot(dek, sid, _v1_bytes())
    audited: list[dict[str, Any]] = []

    class Conn:
        async def execute(self, stmt: Any, params: Any = None) -> Any:
            if "SELECT ciphertext" in str(stmt):
                row = SimpleNamespace(ciphertext=blob, content_hash="x")
                return SimpleNamespace(first=lambda: row)
            audited.append(dict(params or {}))
            return SimpleNamespace()

    got = await verify_snapshot_integrity(Conn(), sid, dek, "x")  # type: ignore[arg-type]
    assert isinstance(got, Mismatch) and got.reason is MismatchReason.UNSUPPORTED_SCHEMA
    assert any("unsupported_schema" in str(p.values()) for p in audited), audited

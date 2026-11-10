"""``python -m vektra_assess.keys verify`` against real Postgres (plan 12b).

A restore with the wrong key set is the case: the rows reference kid ``k1``, and the loaded
keyring either lacks it or holds different bytes under the same name.
"""

from __future__ import annotations

import io

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_assess import keys_verify as kv
from vektra_assess.keys import AssessKeyring

from .conftest import make_real_session, sym, x_ring

pytestmark = pytest.mark.integration


def _query(engine):  # type: ignore[no-untyped-def]
    # the production helper, so a regression in its permission handling is caught here
    return kv.make_query(engine)


async def test_the_right_keys_open_the_rows_and_the_wrong_ones_are_named(pg) -> None:  # type: ignore[no-untyped-def]
    kek = sym("k1")
    pub, _priv = x_ring("h1")
    await make_real_session(pg, kek, pub)
    engine = create_async_engine(pg.url("worker"))
    try:
        good = await kv.verify({"assess_kek": kek}, _query(engine))
        assert not good.mismatched
        assert any(f.status == kv.OK and f.reason == "opens" for f in good.findings)
        out = io.StringIO()
        kv.render(good, out)
        assert "RESULT: ok" in out.getvalue()
        assert (
            kek.keys["k1"].hex() not in out.getvalue()
        )  # a fingerprint, never the key
        assert kv.fingerprint(kek.keys["k1"]) in out.getvalue()

        # same kid, other bytes: a restore from another deployment
        impostor = AssessKeyring("assess_kek", "k1", {"k1": bytes(32)})
        bad = await kv.verify({"assess_kek": impostor}, _query(engine))
        assert bad.mismatched
        assert any(f.reason and f.reason.startswith("wrong_key") for f in bad.findings)

        # the kid is gone altogether (the keyring was rotated and the old key dropped)
        gone = await kv.verify({"assess_kek": sym("k2")}, _query(engine))
        assert [f.reason for f in gone.findings if f.status == kv.MISMATCH] == [
            "kid_not_loaded"
        ]
    finally:
        await engine.dispose()


async def test_a_purpose_the_role_cannot_read_is_skipped_not_passed(pg) -> None:  # type: ignore[no-untyped-def]
    engine = create_async_engine(pg.url("api"))
    try:
        rep = await kv.verify({"assess_kek": sym("k1")}, _query(engine))
        (f,) = [f for f in rep.findings if f.check.startswith("assess_session_keys")]
        assert (f.status, f.reason) == (kv.SKIPPED, "not_readable")
    finally:
        await engine.dispose()


async def test_a_transcript_key_is_opened_on_its_own_row(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit nitpick: the transcript sample query runs against the real schema."""
    import uuid

    from vektra_assess.transcript import store

    kek, tkek = (
        sym("k1"),
        AssessKeyring("assess_transcript_kek", "t1", {"t1": bytes(range(32))}),
    )
    pub, _ = x_ring("h1")
    sid = await make_real_session(pg, kek, pub, close=True)
    tid = uuid.uuid4()
    sealed = store.seal(tkek, tid, b"<html>transcript</html>")
    await pg.run(
        "super",
        "INSERT INTO assess_transcripts (id, session_id, ciphertext, dek_wrapped, "
        "transcript_kid, content_hash, turns_digest, filename, byte_size) "
        "VALUES (:i, :s, :c, :d, :k, :h, 'digest', 't.html', 23)",
        {"i": tid, "s": sid, "c": sealed.ciphertext, "d": sealed.dek_wrapped,
         "k": sealed.kid, "h": sealed.content_sha256},
    )  # fmt: skip
    engine = create_async_engine(pg.url("worker"))
    try:
        ok = await kv.verify({"assess_transcript_kek": tkek}, kv.make_query(engine))
        assert any(
            f.check.startswith("assess_transcripts") and f.reason == "opens"
            for f in ok.findings
        )
        wrong = AssessKeyring("assess_transcript_kek", "t1", {"t1": bytes(32)})
        bad = await kv.verify({"assess_transcript_kek": wrong}, kv.make_query(engine))
        assert bad.mismatched
    finally:
        await engine.dispose()

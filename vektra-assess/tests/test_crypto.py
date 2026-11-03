"""vektra_assess.crypto: envelope, KEK, sealing, teacher config, transcript, hashing
(plan assess-data-crypto 4.7)."""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from structlog.testing import capture_logs

from vektra_assess.crypto import envelope, hashing, seal, teacher_config, transcript
from vektra_assess.crypto.kek import KeyNotAvailable, unwrap_dek, wrap_dek
from vektra_assess.keys import AssessKeyring

CANARY = b"CANARY-plaintext-7f3a"


def ring(name: str = "assess_kek", kids: tuple[str, ...] = ("k1",)) -> AssessKeyring:
    return AssessKeyring(
        name=name, active=kids[-1], keys={k: os.urandom(32) for k in kids}
    )


def x25519_rings() -> tuple[AssessKeyring, AssessKeyring]:
    raw = os.urandom(32)
    pub = seal.public_from_private(raw)
    return (
        AssessKeyring(name="pub", active="k1", keys={"k1": pub}),
        AssessKeyring(name="priv", active="k1", keys={"k1": raw}),
    )


# --- envelope ---------------------------------------------------------------------


def test_envelope_roundtrip_and_format():
    key = envelope.new_dek()
    blob = envelope.aead_encrypt(key, "dek", CANARY, purpose="snapshot", binding=["s1"])
    assert blob[0] == 1 and blob[1] == 3 and blob[2:5] == b"dek"
    assert envelope.peek_kid(blob) == "dek"
    assert (
        envelope.aead_decrypt(key, blob, purpose="snapshot", binding=["s1"]) == CANARY
    )


def test_aad_is_purpose_and_binding():
    assert envelope.aad("turn", ["sid", 3]) == b"vektra-assess/v1/turn/sid/3"
    with pytest.raises(ValueError):
        envelope.aad("nope", ["x"])
    with pytest.raises(ValueError):
        envelope.aad("turn", ["a/b"])
    with pytest.raises(ValueError):
        envelope.aad("turn", [])


def _flip(blob: bytes, index: int) -> bytes:
    return blob[:index] + bytes([blob[index] ^ 1]) + blob[index + 1 :]


@pytest.mark.parametrize("where", ["nonce", "ciphertext", "tag"])
def test_tampering_any_part_fails_authentication(where):
    key = envelope.new_dek()
    blob = envelope.aead_encrypt(key, "dek", CANARY, purpose="turn", binding=["s", 1])
    start = 2 + blob[1]
    index = {"nonce": start, "ciphertext": start + 12, "tag": len(blob) - 1}[where]
    with pytest.raises(InvalidTag):
        envelope.aead_decrypt(key, _flip(blob, index), purpose="turn", binding=["s", 1])


def test_ciphertext_moved_to_another_row_or_column_fails():
    key = envelope.new_dek()
    blob = envelope.encrypt_turn(key, "sess-A", 2, CANARY)
    assert envelope.decrypt_turn(key, "sess-A", 2, blob) == CANARY
    for sid, no in (("sess-B", 2), ("sess-A", 3)):
        with pytest.raises(InvalidTag):
            envelope.decrypt_turn(key, sid, no, blob)
    with pytest.raises(InvalidTag):  # a turn envelope used as a snapshot
        envelope.aead_decrypt(key, blob, purpose="snapshot", binding=["sess-A"])


@pytest.mark.parametrize(
    "blob",
    [b"", b"\x01", b"\x02\x01k" + b"0" * 40, b"\x01\x00" + b"0" * 40, b"\x01\x05ab"],
)
def test_malformed_envelopes_are_rejected_cleanly(blob):
    with pytest.raises(envelope.EnvelopeError):
        envelope.aead_decrypt(envelope.new_dek(), blob, purpose="turn", binding=["s"])


def test_wrong_key_length_is_refused():
    with pytest.raises(ValueError):
        envelope.aead_encrypt(b"short", "k", b"x", purpose="turn", binding=["s"])


def test_nonces_are_unique_over_100k_encryptions():
    key = envelope.new_dek()
    seen = set()
    for _ in range(100_000):
        blob = envelope.aead_encrypt(key, "k", b"x", purpose="turn", binding=["s"])
        seen.add(blob[2 + blob[1] : 2 + blob[1] + 12])
    assert len(seen) == 100_000


# --- KEK ------------------------------------------------------------------------------


def test_wrap_unwrap_and_binding():
    kek, dek = ring(), envelope.new_dek()
    blob, kid, version = wrap_dek(kek, dek, "sess-1", 4)
    assert (kid, version) == ("k1", 4)
    assert unwrap_dek(kek, blob, "sess-1", 4) == dek
    for sid, ver in (("sess-2", 4), ("sess-1", 5)):
        with pytest.raises(InvalidTag):
            unwrap_dek(kek, blob, sid, ver)


def test_unknown_kid_never_falls_back_to_another_key():
    old, dek = ring(kids=("k1",)), envelope.new_dek()
    blob, _, _ = wrap_dek(old, dek, "s")
    rotated = AssessKeyring(name="assess_kek", active="k2", keys={"k2": os.urandom(32)})
    with pytest.raises(KeyNotAvailable):
        unwrap_dek(rotated, blob, "s")
    both = AssessKeyring(
        name="assess_kek", active="k2", keys={**old.keys, **rotated.keys}
    )
    assert unwrap_dek(both, blob, "s") == dek  # the old kid still opens old data


def test_a_known_but_wrong_kid_fails_authentication_not_silently():
    two = ring(kids=("k1", "k2"))
    blob, _, _ = wrap_dek(two, envelope.new_dek(), "s")
    forged = blob[:2] + b"k1" + blob[4:] if blob[2:4] == b"k2" else blob
    with pytest.raises(InvalidTag):
        unwrap_dek(two, forged, "s")


# --- sealing --------------------------------------------------------------------------


def test_seal_roundtrip_binding_and_tamper():
    pub, priv = x25519_rings()
    blob = seal.seal(
        pub.active_key, "k1", CANARY, purpose="handoff", binding=["s", "k1"]
    )
    assert (
        seal.open_sealed(priv, blob, purpose="handoff", binding=["s", "k1"]) == CANARY
    )
    with pytest.raises(InvalidTag):
        seal.open_sealed(priv, blob, purpose="handoff", binding=["s2", "k1"])
    with pytest.raises(InvalidTag):
        seal.open_sealed(priv, blob, purpose="teacher-config", binding=["s", "k1"])
    for index in (2 + blob[1], 2 + blob[1] + 32 + 12, len(blob) - 1):
        with pytest.raises(InvalidTag):
            seal.open_sealed(
                priv, _flip(blob, index), purpose="handoff", binding=["s", "k1"]
            )


def test_sealing_is_randomised_and_wrong_recipient_cannot_open():
    pub, _ = x25519_rings()
    _, other_priv = x25519_rings()
    a = seal.seal(pub.active_key, "k1", b"x", purpose="handoff", binding=["s", "k1"])
    b = seal.seal(pub.active_key, "k1", b"x", purpose="handoff", binding=["s", "k1"])
    assert a != b
    with pytest.raises(InvalidTag):
        seal.open_sealed(other_priv, a, purpose="handoff", binding=["s", "k1"])


def test_seal_known_answer_matches_an_independent_derivation():
    """The primitive construction, recomputed with raw cryptography calls (not our helpers)."""
    eph = X25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    rpriv = bytes(range(101, 133))
    rpub = seal.public_from_private(rpriv)
    nonce = bytes(range(12))
    blob = seal._seal(eph, nonce, rpub, "k1", b"hello", "handoff", ["sess-1", "k1"])

    eph_pub = eph.public_key().public_bytes_raw()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(rpub))
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=eph_pub + rpub,
        info=b"vektra-assess/seal/v1/handoff/k1",
    ).derive(shared)
    ct = AESGCM(key).encrypt(nonce, b"hello", b"vektra-assess/v1/handoff/sess-1/k1")
    assert blob == b"\x01\x02k1" + eph_pub + nonce + ct
    # pinned vector: a change of the wire format or the derivation must be deliberate
    assert blob.hex() == (
        "01026b3107a37cbc142093c8b755dc1b10e86cb426374ad16aa853ed0bdfc0b2b86d1c7c"
        "000102030405060708090a0b4769e1b3bb7b74f30e54a7afd1a149703074661881"
    )
    priv = AssessKeyring(name="p", active="k1", keys={"k1": rpriv})
    assert (
        seal.open_sealed(priv, blob, purpose="handoff", binding=["sess-1", "k1"])
        == b"hello"
    )


def test_open_sealed_rejects_malformed_blobs():
    _, priv = x25519_rings()
    for blob in (
        b"",
        b"\x01\x00",
        b"\x01\x02k1" + b"0" * 10,
        b"\x09\x02k1" + b"0" * 80,
    ):
        with pytest.raises(envelope.EnvelopeError):
            seal.open_sealed(priv, blob, purpose="handoff", binding=["s", "k1"])


# --- teacher config: API can write, cannot read -----------------------------------------


def test_teacher_config_is_write_only_for_the_api_key_set():
    from vektra_assess.keys import KEY_MATRIX, Need

    worker_pub, worker_priv = x25519_rings()
    blob, kid = teacher_config.seal_teacher_config(
        worker_pub, "site-1", 7, "solution", "x = 42"
    )
    assert kid == "k1"
    assert (
        teacher_config.open_teacher_config(worker_priv, blob, "site-1", 7, "solution")
        == "x = 42"
    )
    with pytest.raises(InvalidTag):  # bound to site, cmid and field
        teacher_config.open_teacher_config(worker_priv, blob, "site-1", 8, "solution")
    with pytest.raises(InvalidTag):
        teacher_config.open_teacher_config(worker_priv, blob, "site-1", 7, "rubric")
    # the secret needed to open it is forbidden for the API role by the key matrix
    assert KEY_MATRIX["assess_teacher_config_private"].need("api") is Need.FORBIDDEN
    assert KEY_MATRIX["assess_teacher_config_public"].need("api") is not Need.FORBIDDEN


# --- transcript -------------------------------------------------------------------------


def test_transcript_roundtrip_binding_and_shred_semantics():
    kek = ring("assess_transcript_kek")
    ct, wrapped, kid = transcript.encrypt_transcript(kek, "tr-1", CANARY)
    assert kid == "k1" and CANARY not in ct
    assert transcript.decrypt_transcript(kek, "tr-1", ct, wrapped) == CANARY
    with pytest.raises(InvalidTag):
        transcript.decrypt_transcript(kek, "tr-2", ct, wrapped)
    ct2, wrapped2, _ = transcript.encrypt_transcript(kek, "tr-1", CANARY)
    with pytest.raises(InvalidTag):  # another transcript's DEK does not open this one
        transcript.decrypt_transcript(kek, "tr-1", ct, wrapped2)
    assert ct != ct2


# --- hashing ----------------------------------------------------------------------------


def _turns(genesis: str, texts: list[bytes]) -> list[hashing.ChainTurn]:
    out, prev = [], genesis
    for i, t in enumerate(texts, 1):
        role, kind = ("bot", "opening") if i == 1 else ("student", "normal")
        h = hashing.turn_hash(prev, i, role, kind, t)
        out.append(hashing.ChainTurn(i, role, kind, t, prev, h))
        prev = h
    return out


def test_snapshot_hash_is_canonical():
    assert hashing.snapshot_hash({"b": 1, "a": [1, 2]}) == hashing.snapshot_hash(
        {"a": [1, 2], "b": 1}
    )
    assert hashing.snapshot_hash({"a": "è"}) != hashing.snapshot_hash({"a": "e"})


def test_chain_verifies_and_detects_every_kind_of_break():
    g = hashing.snapshot_hash({"q": 1})
    turns = _turns(g, [b"hello", b"answer", b"more"])
    assert hashing.verify_chain(g, turns) == 3
    forged = [
        turns[0],
        hashing.ChainTurn(
            2, "student", "normal", b"EDITED", turns[1].prev_hash, turns[1].turn_hash
        ),
        turns[2],
    ]
    with pytest.raises(hashing.ChainError) as exc:
        hashing.verify_chain(g, forged)
    assert exc.value.turn_no == 2
    with pytest.raises(hashing.ChainError):  # a turn removed from the middle
        hashing.verify_chain(g, [turns[0], turns[2]])
    with pytest.raises(hashing.ChainError):  # wrong genesis: snapshot swapped
        hashing.verify_chain(hashing.snapshot_hash({"q": 2}), turns)


def test_turn_hash_is_unambiguous_across_field_boundaries():
    assert hashing.turn_hash("p", 1, "ab", "c", b"d") != hashing.turn_hash(
        "p", 1, "a", "bc", b"d"
    )
    assert hashing.turn_hash("p", 1, "r", "k", b"x") != hashing.turn_hash(
        "p", 2, "r", "k", b"x"
    )


# --- no key or plaintext leaks -----------------------------------------------------------


def test_reprs_and_logs_carry_no_key_bytes():
    kek = ring()
    secret_hex = kek.active_key.hex()
    with capture_logs() as logs:
        blob, _, _ = wrap_dek(kek, envelope.new_dek(), "s")
        unwrap_dek(kek, blob, "s")
        ct, wr, _ = transcript.encrypt_transcript(kek, "t", CANARY)
        transcript.decrypt_transcript(kek, "t", ct, wr)
    flat = repr(logs) + repr(kek) + str(kek)
    assert secret_hex not in flat and CANARY.decode() not in flat
    assert str(kek.active_key) not in repr(kek)
    for err in (KeyNotAvailable("x"), InvalidTag()):
        assert CANARY.decode() not in repr(err)


def test_no_key_the_api_holds_opens_teacher_config(tmp_path):
    """Acceptance: sealed by the API key set, opened only by the worker's private key."""
    from vektra_assess.keys import KEY_MATRIX, Kind, Need, generate, load_keyring

    generate(tmp_path)
    worker_pub = load_keyring("assess_teacher_config_public", tmp_path)
    blob, _ = teacher_config.seal_teacher_config(
        worker_pub, "site-1", 7, "config", "secret solution"
    )
    opened = 0
    for name, spec in KEY_MATRIX.items():
        if spec.need("api") is Need.FORBIDDEN or spec.kind not in (
            Kind.SYMMETRIC,
            Kind.X25519_PRIVATE,
            Kind.X25519_PUBLIC,
        ):
            continue
        ring = load_keyring(name, tmp_path)
        with pytest.raises((InvalidTag, KeyNotAvailable)):
            teacher_config.open_teacher_config(ring, blob, "site-1", 7, "config")
        opened += 1
    assert opened >= 4  # the check really iterated the API's keys
    worker_priv = load_keyring("assess_teacher_config_private", tmp_path)
    assert (
        teacher_config.open_teacher_config(worker_priv, blob, "site-1", 7, "config")
        == "secret solution"
    )


def test_open_sealed_reports_a_non_utf8_kid_as_an_envelope_error():
    _, priv = x_ring_for_test()
    bad = b"\x01\x02\xff\xfe" + b"0" * 80
    with pytest.raises(envelope.EnvelopeError):
        seal.open_sealed(priv, bad, purpose="handoff", binding=["s", "k1"])


def test_seal_validates_the_kid_before_doing_any_crypto():
    pub, _ = x_ring_for_test()
    with pytest.raises(ValueError):
        seal.seal(
            pub.active_key, "k" * 300, b"x", purpose="handoff", binding=["s", "k"]
        )


def x_ring_for_test():
    return x25519_rings()

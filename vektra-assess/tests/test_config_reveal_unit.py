"""FEAT-030 "Mostra" without a database: the ephemeral re-seal, the body, the wiring tables.

The end-to-end round trip (real route, real roles, RLS, audit) is
``integration/test_config_reveal.py``.
"""

from __future__ import annotations

import uuid

import pytest

from vektra_assess.crypto import envelope
from vektra_assess.crypto.reveal import (
    REVEAL_KID,
    EphemeralKey,
    public_from_hex,
    seal_reveal,
)
from vektra_assess.jobs import JobKind
from vektra_assess.worker.policy import KIND_DEFAULTS
from vektra_assess.worker.slots import HANDLER_OWNERS

SITE = uuid.uuid4()
RID = uuid.uuid4()


def test_the_ephemeral_key_opens_only_its_own_binding() -> None:
    eph = EphemeralKey.generate()
    pub = public_from_hex(eph.public_hex)
    assert pub is not None and len(pub) == 32
    blob = seal_reveal(pub, b"segreto", SITE, 5, "solution", RID)
    assert b"segreto" not in blob
    assert blob[2 : 2 + blob[1]].decode() == REVEAL_KID
    assert eph.open(blob, SITE, 5, "solution", RID) == b"segreto"
    for other in (
        (uuid.uuid4(), 5, "solution", RID),
        (SITE, 6, "solution", RID),
        (SITE, 5, "pasted_rubric", RID),
        (SITE, 5, "solution", uuid.uuid4()),
    ):
        with pytest.raises(envelope.InvalidTag):
            eph.open(blob, *other)
    with pytest.raises(envelope.InvalidTag):
        EphemeralKey.generate().open(blob, SITE, 5, "solution", RID)


def test_the_purpose_is_its_own() -> None:
    """A teacher-config blob is never accepted as a reveal blob, nor the reverse."""
    assert envelope.Purpose.CONFIG_REVEAL == "config-reveal"
    assert envelope.Purpose.CONFIG_REVEAL in envelope.Purpose.ALL
    assert envelope.Purpose.CONFIG_REVEAL != envelope.Purpose.TEACHER_CONFIG


def test_no_key_material_in_the_repr() -> None:
    assert repr(EphemeralKey.generate()) == "EphemeralKey(<redacted>)"


@pytest.mark.parametrize(
    "value", ["", "00" * 31, "00" * 33, "zz" * 32, "0x" + "00" * 31, None, 7]
)
def test_a_public_key_is_64_hex_digits_or_nothing(value: object) -> None:
    assert public_from_hex(value) is None


def test_the_job_kind_has_an_owner_and_a_short_single_attempt_policy() -> None:
    assert JobKind.CONFIG_REVEAL.value == "config_reveal"
    assert JobKind.CONFIG_REVEAL in HANDLER_OWNERS
    policy = KIND_DEFAULTS[JobKind.CONFIG_REVEAL]
    # one attempt: the request waits a few seconds, a retry would answer nobody
    assert policy.max_attempts == 1 and policy.pool == "maintenance"


def test_the_reveal_body_names_one_field() -> None:
    from vektra_assess.teacher.schemas import ConfigReveal

    ok = ConfigReveal.model_validate(
        {"actor_userid": 3, "courseid": 4, "activity_type": "quiz", "field": "solution"}
    )
    assert ok.field == "solution"
    # the field itself is checked by the route (400 ``field``), never coerced here
    loose = ConfigReveal.model_validate(
        {"actor_userid": 3, "courseid": 4, "activity_type": "quiz"}
    )
    assert loose.field is None


def test_the_wait_stays_under_the_plugins_own_four_seconds() -> None:
    """The plugin waits 4 s per call (one retry): the API answers 503 before that."""
    from pydantic import ValidationError

    from vektra_assess.config import VektraAssessSettings

    assert VektraAssessSettings().config_reveal_timeout_s == 3.0
    with pytest.raises(ValidationError):
        VektraAssessSettings.model_validate(
            {"VEKTRA_ASSESS_CONFIG_REVEAL_TIMEOUT_S": 4.0}
        )


def test_the_refusals_carry_the_standard_envelope_and_a_retry_hint() -> None:
    from vektra_assess.errors import config_field_not_set, config_reveal_unavailable
    from vektra_shared.errors import http_status_for

    gone = config_field_not_set("solution")
    assert (gone.code, http_status_for(gone)) == ("ERR-ASSESS-030", 404)
    assert gone.details == {"reason": "field_not_set", "field": "solution"}
    busy = config_reveal_unavailable()
    assert (busy.code, http_status_for(busy)) == ("ERR-ASSESS-031", 503)
    assert busy.details == {"reason": "reveal_unavailable", "retry_after_s": 2}
    assert busy.retry_after == 2

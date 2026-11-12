"""Request and response models of the student session routes (INT-001, ARCH-067).

Requests are strict (``extra="forbid"``): an unknown field is a 400, so a client can never
smuggle an LMS attempt id or a client clock into a decision (BR-012, REQ-069). The login-as
fields are REQUIRED and checked positively by the mint route: absence is a refusal, not a default.
Responses carry ids, states, timestamps and codes; the only text they ever carry is a session's
own decrypted turns, returned to its owner.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SessionState = Literal["STARTING", "ACTIVE", "OUTAGE", "CLOSED"]

#: an LMS activity id as the plugin sends it: a positive integer below 2**53, lax like every
#: other body int (PHP json_encode may send "501"); one definition for every body that takes one
Cmid = Annotated[int, Field(gt=0, lt=2**53)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MintRequest(_Strict):
    """``POST /tokens`` body sent by the plugin (discuss-student plan, step 3).

    ``loginas`` and ``realuser_id`` are typed loosely ON PURPOSE: the route asserts
    ``loginas is False`` and ``realuser_id is None`` itself, so that ``true``, ``1``, a missing
    field or any non-null value is audited as ``loginas_rejected`` rather than bouncing off the
    schema as a generic 400.
    """

    sub: int = Field(gt=0, lt=2**53)
    cmid: Cmid
    courseid: int | None = Field(default=None, gt=0, lt=2**53)  # a hint, re-resolved
    loginas: Any = None
    realuser_id: Any = None
    plugin_version: str = Field(min_length=1, max_length=40)

    def asserts_not_loginas(self) -> bool:
        """The positive REQ-084 assertion: both fields PRESENT, ``false`` and ``null`` exactly."""
        present = {"loginas", "realuser_id"} <= self.model_fields_set
        return present and self.loginas is False and self.realuser_id is None


class MintResponse(BaseModel):
    token: str
    expires_in: int
    jti: str


class StartRequest(_Strict):
    """``POST /sessions``: nothing but the bearer token decides; the body is empty or ``{}``."""


class StartResponse(BaseModel):
    session_id: str
    state: SessionState
    token: str
    session_no: int


class TurnView(BaseModel):
    turn_no: int
    role: str
    kind: str
    text: str


class SessionView(BaseModel):
    session_id: str
    state: SessionState
    close_reason: str | None
    session_no: int
    deadline_at: datetime | None
    server_now: datetime
    committed_at: datetime | None
    reply_pending: bool
    turns: list[TurnView]
    token: str | None = None  # re-minted on the resume path only


class TurnRequest(_Strict):
    text: str = Field(min_length=1)


class TurnAccepted(BaseModel):
    session_id: str
    turn_no: int


class StateResponse(BaseModel):
    """``GET /state``: the advisory button state for (token sub, cmid) and a reason code."""

    state: str
    reason: str | None
    session_id: str | None = None
    computed_at: datetime | None = None

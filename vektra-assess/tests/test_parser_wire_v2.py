"""The parser wire of PR 2a-2, after review (findings 1, 4 and 10): the profile fits the request
header, a request the client cannot frame degrades, the profile is validated once per profile,
and the protocol version moved with the ``review_item`` format."""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_parser_client import (  # noqa: F401 (``fake`` is a fixture)
    PROFILE,
    Fake,
    client_for,
    fake,
    lie,
    reply,
)

from vektra_assess.parser_protocol import (
    MAX_DROP_CLASSES,
    MAX_HEADER,
    MAX_PROFILE_BYTES,
    PROTOCOL_VERSION,
    FileStatus,
    ReviewProfile,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")


def test_a_profile_within_the_per_class_limits_but_over_the_header_is_refused() -> None:
    """64 classes of 64 characters pass every per-class rule but overflow the 4096-byte
    request header: ``build`` refuses it on its encoded size, never truncates it."""
    drop = [f"c{i:02d}" + "x" * 60 for i in range(MAX_DROP_CLASSES)]
    wire = {"keep": {"question_text": "qtext"}, "drop": drop, "version": "v1"}
    assert len(json.dumps(wire)) > MAX_HEADER  # the overflow is real
    with pytest.raises(ValueError):
        ReviewProfile.build({"question_text": "qtext"}, drop, "v1")
    assert ReviewProfile.from_wire(wire) is None
    ok = ReviewProfile.build({"question_text": "qtext"}, drop[:8], "v1")
    assert ok.encoded_size() <= MAX_PROFILE_BYTES


async def test_a_request_the_client_cannot_frame_degrades_instead_of_raising(
    fake: Fake,  # noqa: F811
) -> None:
    """A header over ``MAX_HEADER`` used to raise ``FrameError`` out of ``_call`` and roll the
    Start back. Now it is CORRUPT for that item with its own reason, found before any
    connection (second review, finding 5): the sidecar is never blamed for this request."""
    import asyncio

    connects: list[object] = []
    real = asyncio.open_unix_connection

    async def counting(*a: Any, **k: Any) -> Any:
        connects.append(a)
        return await real(*a, **k)

    asyncio.open_unix_connection = counting  # type: ignore[assignment]
    try:
        res = await client_for(fake).parse_file(b"abc", "x" * 5000)
    finally:
        asyncio.open_unix_connection = real  # type: ignore[assignment]
    assert res.status is FileStatus.CORRUPT and res.code == "request_too_large"
    assert connects == [] and fake.requests == []


async def test_a_set_of_drop_classes_is_cached_like_a_frozenset(
    fake: Fake,  # noqa: F811
) -> None:
    """Second review, finding 2: a provider handing a ``set`` used to make the cache key
    unhashable. The profile freezes it, so the item is extracted and the cache works."""
    import dataclasses

    fake.responder = lie(
        lambda r: reply(
            r,
            text=json.dumps(
                {"question_text": "Q", "response": "A", "truncated": False}
            ),
            code="review_item",
        )
    )
    profile = dataclasses.replace(PROFILE, drop_classes={"q-solution"})  # type: ignore[arg-type]
    client = client_for(fake)
    for _ in range(2):
        assert await client.extract_review_item(b"<p>x</p>", profile) == (
            "Q",
            "A",
            False,
        )
    assert len(client._profiles) == 1


async def test_the_profile_wire_form_is_built_once_per_profile(
    fake: Fake,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str] = []
    real = ReviewProfile.build.__func__  # type: ignore[attr-defined]

    def counting(cls: type, *a: Any, **k: Any) -> Any:
        built.append(a[2])
        return real(cls, *a, **k)

    monkeypatch.setattr(ReviewProfile, "build", classmethod(counting))
    fake.responder = lie(
        lambda r: reply(
            r,
            text=json.dumps(
                {"question_text": "Q", "response": "A", "truncated": False}
            ),
            code="review_item",
        )
    )
    client = client_for(fake)
    for _ in range(3):
        assert await client.extract_review_item(b"<p>x</p>", PROFILE) == (
            "Q",
            "A",
            False,
        )
    assert built == ["test-1"]
    assert all(r["profile"] == fake.requests[0]["profile"] for r in fake.requests)


async def test_a_sidecar_of_the_previous_protocol_is_refused(
    fake: Fake,  # noqa: F811
) -> None:
    """The review_item wire format changed, so the version did: a reply of the previous
    version (an old sidecar) is a violation, never parsed."""
    assert PROTOCOL_VERSION == 2
    fake.responder = lie(lambda r: reply(r, v=PROTOCOL_VERSION - 1, text="hi"))
    res = await client_for(fake).parse_file(b"abc", "txt")
    assert res.status is FileStatus.CORRUPT and res.text == ""

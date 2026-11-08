"""Plan 10b ``delivery/recipients.py``: the configured addresses, their refs, masks and domains."""

from __future__ import annotations

import hashlib
import json

import pytest

from vektra_assess.delivery import recipients as r


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("prof@uni.example", "prof@uni.example"),
        ("  Prof.Rossi@Uni.EXAMPLE ", "Prof.Rossi@uni.example"),
        ("a+tag@mail.uni.example", "a+tag@mail.uni.example"),
        ("x@localhost", None),  # a domain needs a dot
        ("Prof <p@uni.example>", None),  # display name
        ("p@uni.example, q@uni.example", None),
        ("p q@uni.example", None),
        ("p@@uni.example", None),
        ("p@uni.example\r\nBcc: x@evil.example", None),
        ("p\x00@uni.example", None),
        (".p@uni.example", None),
        ("p..q@uni.example", None),
        ("p@-uni.example", None),
        ("p@uni_x.example", None),
        ("p@unicàl.it", None),  # non-ASCII domain: refused, not converted
        ("x" * 65 + "@uni.example", None),
        ("p@" + "a" * 250 + ".it", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_normalize_address(raw: object, want: str | None) -> None:
    assert r.normalize_address(raw) == want


def test_configured_recipients_dedupes_and_caps_at_three() -> None:
    doc = {
        r.RECIPIENTS_KEY: [
            "a@uni.example",
            "bad",
            "a@UNI.example",  # same address once the domain is lowercased
            "b@uni.example",
            "c@uni.example",
            "d@uni.example",
        ],
        "solution": "never read here",
    }
    want = ("a@uni.example", "b@uni.example", "c@uni.example")
    assert r.configured_recipients(doc) == want
    assert r.configured_recipients(json.dumps(doc)) == want
    # revision 0036: the opened ``transcript_recipients`` field is the list itself
    assert r.configured_recipients(doc[r.RECIPIENTS_KEY]) == want
    assert r.configured_recipients(json.dumps(doc[r.RECIPIENTS_KEY])) == want
    for empty in (None, "", "not json", "[]", {}, {r.RECIPIENTS_KEY: "a@uni.example"}):
        assert r.configured_recipients(empty) == ()  # type: ignore[arg-type]


def test_the_ref_is_sha256_of_site_and_address() -> None:
    ref = r.recipient_ref("site-1", "a@uni.example")
    assert ref == hashlib.sha256(b"site-1|a@uni.example").hexdigest()
    assert len(ref) == 64 and ref != r.recipient_ref("site-2", "a@uni.example")


def test_mask_and_state_view_never_show_the_local_part() -> None:
    assert r.mask("mario.rossi@uni.example") == "m***@uni.example"
    assert r.state_view(["mario.rossi@uni.example"]) == [
        {"role": "recipient", "name": "m***@uni.example"}
    ]


def test_the_domain_allowlist_is_exact_and_empty_refuses_all() -> None:
    domains = r.parse_domains(" Uni.example, ,dept.uni.example ")
    assert domains == frozenset({"uni.example", "dept.uni.example"})
    assert r.domain_allowed("a@uni.example", domains)
    assert r.domain_allowed("a@dept.uni.example", domains)
    assert not r.domain_allowed("a@mat.uni.example", domains)  # no suffix matching
    assert not r.domain_allowed("a@uni.example.evil.example", domains)
    assert not r.domain_allowed("a@uni.example", r.parse_domains(""))
    assert not r.domain_allowed("a@uni.example", r.parse_domains(None))


@pytest.mark.parametrize(
    "raw",
    [
        "p\n@uni.example",  # CodeRabbit 4504367030: `$` matches before a trailing newline
        "p\r\n@uni.example",
        "p@uni.example\n",  # stripped, but the domain must fully match too
        "p\ud800@uni.example",  # CodeRabbit 4504362586: a lone surrogate cannot be encoded
    ],
)
def test_newlines_and_unencodable_text_are_refused(raw: str) -> None:
    got = r.normalize_address(raw)
    assert got is None or (got == "p@uni.example" and raw == "p@uni.example\n")

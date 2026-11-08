"""Plan 10b ``delivery/recipients.py``: the configured addresses, their refs, masks and domains."""

from __future__ import annotations

import hashlib
import json

import pytest

from vektra_assess.delivery import recipients as r


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("prof@unical.it", "prof@unical.it"),
        ("  Prof.Rossi@UniCal.IT ", "Prof.Rossi@unical.it"),
        ("a+tag@mail.unical.it", "a+tag@mail.unical.it"),
        ("x@localhost", None),  # a domain needs a dot
        ("Prof <p@unical.it>", None),  # display name
        ("p@unical.it, q@unical.it", None),
        ("p q@unical.it", None),
        ("p@@unical.it", None),
        ("p@unical.it\r\nBcc: x@evil.example", None),
        ("p\x00@unical.it", None),
        (".p@unical.it", None),
        ("p..q@unical.it", None),
        ("p@-unical.it", None),
        ("p@unical_x.it", None),
        ("p@unicàl.it", None),  # non-ASCII domain: refused, not converted
        ("x" * 65 + "@unical.it", None),
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
            "a@unical.it",
            "bad",
            "a@UNICAL.it",  # same address once the domain is lowercased
            "b@unical.it",
            "c@unical.it",
            "d@unical.it",
        ],
        "solution": "never read here",
    }
    want = ("a@unical.it", "b@unical.it", "c@unical.it")
    assert r.configured_recipients(doc) == want
    assert r.configured_recipients(json.dumps(doc)) == want
    for empty in (None, "", "not json", "[]", {}, {r.RECIPIENTS_KEY: "a@unical.it"}):
        assert r.configured_recipients(empty) == ()  # type: ignore[arg-type]


def test_the_ref_is_sha256_of_site_and_address() -> None:
    ref = r.recipient_ref("site-1", "a@unical.it")
    assert ref == hashlib.sha256(b"site-1|a@unical.it").hexdigest()
    assert len(ref) == 64 and ref != r.recipient_ref("site-2", "a@unical.it")


def test_mask_and_state_view_never_show_the_local_part() -> None:
    assert r.mask("mario.rossi@unical.it") == "m***@unical.it"
    assert r.state_view(["mario.rossi@unical.it"]) == [
        {"role": "recipient", "name": "m***@unical.it"}
    ]


def test_the_domain_allowlist_is_exact_and_empty_refuses_all() -> None:
    domains = r.parse_domains(" Unical.it, ,dimes.unical.it ")
    assert domains == frozenset({"unical.it", "dimes.unical.it"})
    assert r.domain_allowed("a@unical.it", domains)
    assert r.domain_allowed("a@dimes.unical.it", domains)
    assert not r.domain_allowed("a@mat.unical.it", domains)  # no suffix matching
    assert not r.domain_allowed("a@unical.it.evil.example", domains)
    assert not r.domain_allowed("a@unical.it", r.parse_domains(""))
    assert not r.domain_allowed("a@unical.it", r.parse_domains(None))


@pytest.mark.parametrize(
    "raw",
    [
        "p\n@unical.it",  # CodeRabbit 4504367030: `$` matches before a trailing newline
        "p\r\n@unical.it",
        "p@unical.it\n",  # stripped, but the domain must fully match too
        "p\ud800@unical.it",  # CodeRabbit 4504362586: a lone surrogate cannot be encoded
    ],
)
def test_newlines_and_unencodable_text_are_refused(raw: str) -> None:
    got = r.normalize_address(raw)
    assert got is None or (got == "p@unical.it" and raw == "p@unical.it\n")

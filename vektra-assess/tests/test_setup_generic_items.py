"""Vektra's own setup-check items (ADR-0033 D3), on ``FakeLmsProvider``: the UI-origin items and
the same-site rule they share, decided by the vertical whatever the LMS.

These ran only through the Moodle provider's recorded suite until 2a-4 moved it to the adapter;
the decisions are the vertical's, so they are tested here on neutral values. The site is the
fake's ``https://lms.example.org``.
"""

from __future__ import annotations

from typing import Any

import pytest
from support.fake_lms import SITE, FakeLmsProvider

from vektra_assess.config import VektraAssessSettings
from vektra_assess.setup import evaluate
from vektra_assess.setup.items import same_site
from vektra_assess.ui.origins import url_origin

ENV = {
    "VEKTRA_ASSESS_ENABLED": "true",
    "VEKTRA_ASSESS_UI_ORIGIN": "https://chat.vektra-ui.test",
    "VEKTRA_ASSESS_API_PUBLIC_ORIGIN": "https://api.vektra-api.test",
}


def settings(**env: str) -> VektraAssessSettings:
    return VektraAssessSettings(**{**ENV, **env})  # type: ignore[arg-type]


def without(*fragments: str) -> VektraAssessSettings:
    return VektraAssessSettings(  # type: ignore[arg-type]
        **{k: v for k, v in ENV.items() if not any(f in k for f in fragments)}
    )


async def items(
    cfg: VektraAssessSettings | None = None, **kw: Any
) -> dict[str, tuple[str, str | None]]:
    out = await evaluate(FakeLmsProvider(), SITE, cfg or settings(), None, **kw)
    return {i.name: (i.result, i.reason_code) for i in out.items}


def test_same_site_rules() -> None:
    assert same_site("https://lms.example.org", "https://lms.example.org:8443")
    assert same_site("https://chat.example.org", "https://example.org")  # subdomain
    assert same_site("https://chat.example.org", "https://lms.example.org")  # siblings
    assert same_site("http://localhost:3000", "http://localhost:8080")
    # no public-suffix list: it errs towards "same site", a refusal an operator can read
    assert same_site("https://a.example.co.uk", "https://b.example.co.uk")
    assert not same_site("https://chat.vektra-ui.test", "https://lms.example.org")
    assert not same_site(
        "http://10.0.0.1", "http://10.0.0.2"
    )  # IP literals: equal only
    assert same_site("http://10.0.0.1:81", "http://10.0.0.1:82")
    assert same_site("https://EXAMPLE.org.", "https://example.org")
    with pytest.raises(ValueError):
        same_site("ftp://example.org", "https://example.org")


@pytest.mark.parametrize(
    ("ui", "api", "want"),
    [
        (
            "https://lms.example.org",
            "https://api.vektra-api.test",
            ("MISSING", "same_site_as_lms"),
        ),
        (
            "https://chat.vektra-ui.test",
            "https://api.vektra-ui.test",
            ("MISSING", "same_site_as_api"),
        ),
        ("https://chat.vektra-ui.test", "https://chat2.other.test", ("OK", None)),
    ],
)
async def test_the_ui_must_be_another_site_than_the_lms_and_the_api(
    ui: str, api: str, want: tuple[str, str | None]
) -> None:
    got = await items(
        settings(VEKTRA_ASSESS_UI_ORIGIN=ui, VEKTRA_ASSESS_API_PUBLIC_ORIGIN=api)
    )
    assert got["ui_not_same_origin"] == want


async def test_an_unset_origin_is_unverifiable_never_ok() -> None:
    assert (await items(without("UI_ORIGIN")))["ui_not_same_origin"] == (
        "UNVERIFIABLE",
        "ui_origin_unset",
    )
    assert (await items(without("API_PUBLIC")))["ui_not_same_origin"] == (
        "UNVERIFIABLE",
        "api_origin_unset",
    )


async def test_a_malformed_ui_origin_is_refused_when_enabled_and_reported_when_dormant() -> (
    None
):
    """Plan 9: enabled, the settings refuse it at load; disabled they only warn and keep the
    value, and both UI items report it (review #7)."""
    with pytest.raises(ValueError):
        settings(VEKTRA_ASSESS_UI_ORIGIN="not-a-url")
    with pytest.warns(UserWarning):
        dormant = settings(
            VEKTRA_ASSESS_ENABLED="false", VEKTRA_ASSESS_UI_ORIGIN="not-a-url"
        )
    got = await items(dormant)
    assert got["ui_not_same_origin"] == ("UNVERIFIABLE", "origin_invalid")
    assert got["ui_origin_distinct"] == ("MISSING", "ui_origin_invalid")


async def test_the_ui_origin_is_stored_normalised() -> None:
    assert settings(
        VEKTRA_ASSESS_UI_ORIGIN="HTTPS://Chat.Vektra-UI.test:443/"
    ).ui_origin == ("https://chat.vektra-ui.test")


async def test_ui_origin_distinct() -> None:
    """Plan 9 (ARCH-076): OK for a UI origin distinct from the site's framing origin (https
    outside development); MISSING when unset, equal to it, or not https."""
    assert (await items())["ui_origin_distinct"] == ("OK", None)
    assert (await items(without("UI_ORIGIN")))["ui_origin_distinct"] == (
        "MISSING",
        "ui_origin_unset",
    )
    lms = url_origin(SITE.base_url)
    assert (await items(settings(VEKTRA_ASSESS_UI_ORIGIN=lms)))[
        "ui_origin_distinct"
    ] == (
        "MISSING",
        "ui_origin_equals_lms_origin",
    )
    prod = settings(
        VEKTRA_ENV="production", VEKTRA_ASSESS_UI_ORIGIN="http://ui.example"
    )
    assert (await items(prod))["ui_origin_distinct"] == (
        "MISSING",
        "ui_origin_not_https",
    )


async def test_ui_origin_distinct_uses_the_sites_lms_origin_column() -> None:
    """Review #3: the same framing origin as the UI router (``lms_origin``, else the origin of
    ``base_url``), through the same normalisation; another SITE than both."""
    got = await items(lms_origin="HTTPS://Chat.Vektra-UI.test:443")
    assert got["ui_origin_distinct"] == ("MISSING", "ui_origin_equals_lms_origin")
    lms = url_origin(SITE.base_url)
    got = await items(
        settings(VEKTRA_ASSESS_UI_ORIGIN=lms), lms_origin="https://lms-frame.example"
    )
    assert got["ui_origin_distinct"] == ("MISSING", "same_site_as_lms")
    got = await items(
        settings(VEKTRA_ASSESS_UI_ORIGIN="https://chat.lms-frame.example"),
        lms_origin="https://lms-frame.example",
    )
    assert got["ui_origin_distinct"] == ("MISSING", "same_site_as_lms_origin")
    got = await items(lms_origin="https://lms-frame.example")
    assert got["ui_origin_distinct"] == ("OK", None)
    # a stored origin that cannot be used is reported, not skipped (CodeRabbit 4501070007)
    got = await items(lms_origin="https://u:p@lms.example")
    assert got["ui_origin_distinct"] == ("MISSING", "lms_origin_invalid")


async def test_an_item_that_raises_or_answers_outside_the_vocabulary_is_unverifiable() -> (
    None
):
    """The runner never lets one item's failure stop the report, and never reports a result the
    report does not define (``check_error``, fail closed)."""
    from vektra_assess import setup as sc

    reg = sc.builtin_registry()
    pluggable = list(sc.PLUGGABLE_ITEMS)

    async def boom(_ctx: Any) -> sc.Outcome:
        raise RuntimeError("password=hunter2")

    async def odd(_ctx: Any) -> Any:
        return sc.Outcome("MAYBE", None)

    reg.register(pluggable[0], boom)
    reg.register(pluggable[1], odd)
    got = await items(registry=reg)
    assert got[pluggable[0]] == ("UNVERIFIABLE", "check_error")
    assert got[pluggable[1]] == ("UNVERIFIABLE", "check_error")

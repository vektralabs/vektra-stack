"""The generic setup-check runner (``vektra_assess.setup``, ADR-0033 D3) on ``FakeLmsProvider``.

What the runner decides on its own, whatever the provider: the report order, the fail-closed
answers for a declared item that is not returned, for a provider that raises or declares names it
may not, for a missing provider, and for a capability flag that is not returned.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from support.fake_lms import SITE, FakeLmsProvider

from vektra_assess.capabilities import FLAGS
from vektra_assess.config import VektraAssessSettings
from vektra_assess.setup import (
    GENERIC_ITEMS,
    MISSING,
    OK,
    PLUGGABLE_ITEMS,
    UNVERIFIABLE,
    evaluate,
    probe,
)
from vektra_shared.lms import (
    PROVIDER_ERROR,
    CapabilityResult,
    LmsSite,
    LmsSiteInfo,
    Reading,
    SetupItem,
    Unreadable,
    UnreadableReason,
)

SETTINGS = VektraAssessSettings(VEKTRA_ASSESS_ENABLED="true")  # type: ignore[arg-type]


def _items(out: Any) -> dict[str, tuple[str, str | None]]:
    return {i.name: (i.result, i.reason_code) for i in out.items}


async def _run(provider: Any) -> Any:
    return await evaluate(provider, SITE, SETTINGS, None)


async def test_the_report_order_is_provider_items_then_vektras() -> None:
    fake = FakeLmsProvider(setup_items=("token_works", "required_reads"))
    out = await _run(fake)
    assert [i.name for i in out.items] == [
        "token_works",
        "required_reads",
        *GENERIC_ITEMS,
        *PLUGGABLE_ITEMS,
    ]
    items = _items(out)
    assert items["token_works"] == (OK, None)
    assert items["lms_provider_registered"] == (OK, None)
    assert out.release == fake.world.site_info.release
    assert [f.flag for f in out.flags] == list(FLAGS)


async def test_without_a_provider_the_item_says_so() -> None:
    out = await _run(None)
    items = _items(out)
    assert items["lms_provider_registered"] == (MISSING, "not_registered")
    assert out.release is None and out.flags == () and not out.token_rejected
    assert [i.name for i in out.items] == [*GENERIC_ITEMS, *PLUGGABLE_ITEMS]


class _Drops(FakeLmsProvider):
    """Declares two items, returns one (and one it never declared)."""

    async def service_account_check(  # type: ignore[override]
        self,
        site: LmsSite,
        info: Reading[LmsSiteInfo],
        manifest: Mapping[str, Any] | None,
    ) -> tuple[SetupItem, ...]:
        return (SetupItem("token_works", OK), SetupItem("smuggled", OK))


async def test_a_declared_item_not_returned_is_missing() -> None:
    out = await _run(_Drops(setup_items=("token_works", "required_reads")))
    items = _items(out)
    assert items["token_works"] == (OK, None)
    assert items["required_reads"] == (MISSING, "not_returned")
    assert "smuggled" not in items  # an undeclared item never enters the report


class _Raises(FakeLmsProvider):
    async def service_account_check(  # type: ignore[override]
        self, site: Any, info: Any, manifest: Any
    ) -> tuple[SetupItem, ...]:
        raise RuntimeError("provider bug with a secret in it")


async def test_a_provider_that_raises_fails_every_declared_item_closed() -> None:
    out = await _run(_Raises(setup_items=("token_works", "required_reads")))
    items = _items(out)
    assert items["token_works"] == (UNVERIFIABLE, "check_error")
    assert items["required_reads"] == (UNVERIFIABLE, "check_error")
    assert not out.all_ok


class _BadResult(FakeLmsProvider):
    async def service_account_check(  # type: ignore[override]
        self, site: Any, info: Any, manifest: Any
    ) -> tuple[SetupItem, ...]:
        return (SetupItem("token_works", "PROBABLY_FINE"),)


async def test_a_result_outside_the_vocabulary_is_unverifiable() -> None:
    out = await _run(_BadResult(setup_items=("token_works",)))
    assert _items(out)["token_works"] == (UNVERIFIABLE, "check_error")


async def test_untrustworthy_declared_names_unregister_the_provider() -> None:
    for names in (
        ("Bad Name",),
        ("token_works", "token_works"),  # a duplicate
        ("ui_origin_distinct",),  # a Vektra item the provider may not override
        ("smtp_probe",),  # a pluggable item likewise
    ):
        out = await _run(FakeLmsProvider(setup_items=names))
        items = _items(out)
        assert items["lms_provider_registered"] == (MISSING, "not_registered"), names
        assert not out.all_ok


class _SiteInfoRaises(FakeLmsProvider):
    async def site_info(self, site: LmsSite) -> Reading[LmsSiteInfo]:  # type: ignore[override]
        raise RuntimeError("boom")


async def test_site_info_raising_is_a_provider_error_not_a_crash() -> None:
    out = await _run(_SiteInfoRaises(setup_items=("token_works",)))
    assert out.release is None and out.flags == ()
    assert not out.token_rejected  # a bug is not "the LMS rejected the token"
    # the fake fails its items closed on an unreadable site_info
    assert _items(out)["token_works"][0] == UNVERIFIABLE


class _Recorded(FakeLmsProvider):
    async def service_account_check(  # type: ignore[override]
        self, site: Any, info: Any, manifest: Any
    ) -> tuple[SetupItem, ...]:
        self.seen_info = info
        return await super().service_account_check(site, info, manifest)


async def test_site_info_raising_reaches_the_items_as_provider_error() -> None:
    class _Both(_SiteInfoRaises, _Recorded):
        pass

    fake = _Both(setup_items=("token_works",))
    await _run(fake)
    assert fake.seen_info == Unreadable(UnreadableReason.UNKNOWN_SHAPE, PROVIDER_ERROR)


class _ShortProbe(FakeLmsProvider):
    async def probe_capabilities(  # type: ignore[override]
        self, site: LmsSite, manifest: Any, flags: Sequence[str]
    ) -> tuple[CapabilityResult, ...]:
        first = flags[0]
        return (
            CapabilityResult(first, "readable", None, "a" * 64),
            CapabilityResult("not_a_flag", "readable", None, "b" * 64),
            CapabilityResult(
                flags[1], "sure", None, "c" * 64
            ),  # outside the vocabulary
        )


async def test_a_flag_the_provider_does_not_return_is_not_probed() -> None:
    got = await probe(_ShortProbe(), SITE, None)
    assert [r.flag for r in got] == list(FLAGS)
    assert got[0].state == "readable"
    for r in got[1:]:
        assert (r.state, r.reason_code) == ("unreadable", "not_probed"), r.flag
        assert len(r.evidence_hash) == 64


class _ProbeRaises(FakeLmsProvider):
    async def probe_capabilities(  # type: ignore[override]
        self, site: Any, manifest: Any, flags: Any
    ) -> tuple[CapabilityResult, ...]:
        raise RuntimeError("boom")


async def test_a_probe_that_raises_is_unreadable_for_every_flag() -> None:
    got = await probe(_ProbeRaises(), SITE, None)
    assert [(r.flag, r.state, r.reason_code) for r in got] == [
        (f, "unreadable", "unknown_shape") for f in FLAGS
    ]


async def test_a_rejected_token_is_reported_and_a_local_refusal_is_not() -> None:
    fake = FakeLmsProvider(setup_items=("token_works",))
    fake.fail(
        "site_info", Unreadable(UnreadableReason.ACCESS_EXCEPTION, "invalidtoken")
    )
    out = await _run(fake)
    assert out.token_rejected and out.token_reason == "invalidtoken"
    fake.clear()
    fake.fail("site_info", Unreadable(UnreadableReason.ACCESS_EXCEPTION, "no_token"))
    out = await _run(fake)
    assert not out.token_rejected

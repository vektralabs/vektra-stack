"""Plan 9: the discussion UI page and its assets (ARCH-076), on a real database."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
from support.world import world

pytestmark = pytest.mark.integration

PAGE = "/api/v1/assess/ui"


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    (tmp_path / "assess-ui.abc123.js").write_text("console.log(1);\n")
    (tmp_path / "assess-ui.def456.css").write_text("body{}\n")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"js": "assess-ui.abc123.js", "css": "assess-ui.def456.css"})
    )
    (tmp_path / "secret.txt").write_text("not an asset")
    return tmp_path


def cfg_of(html: str) -> dict:  # type: ignore[type-arg]
    block = re.search(
        r'<script type="application/json" id="cfg">(.*?)</script>', html, re.S
    )
    assert block is not None
    return json.loads(block.group(1))  # type: ignore[no-any-return]


async def test_the_page_carries_the_site_origin_and_the_hashed_assets(
    pg, bundle
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_ui_static_dir=str(bundle)) as w:
        r = await w.client.get(PAGE, params={"site": str(w.site)})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["cache-control"] == "no-store"
        assert cfg_of(r.text) == {
            "apiBase": "/api/v1/assess",
            "moodleOrigin": "https://m.example",
            "site": str(w.site),
        }
        assert f"{PAGE}/assets/assess-ui.abc123.js" in r.text
        assert f"{PAGE}/assets/assess-ui.def456.css" in r.text
        assert "token" not in r.text.lower()


@pytest.mark.parametrize("site", ["", "not-a-uuid", str(uuid.uuid4())])
async def test_an_unknown_or_malformed_site_is_404(pg, bundle, site: str) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_ui_static_dir=str(bundle)) as w:
        r = await w.client.get(PAGE, params={"site": site})
        assert r.status_code == 404 and r.text == ""


async def test_a_disabled_site_is_404(pg, bundle) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_ui_static_dir=str(bundle)) as w:
        await pg.run(
            "super",
            "UPDATE assess_sites SET disabled_at = now() WHERE id = :s",
            {"s": w.site},
        )
        assert (
            await w.client.get(PAGE, params={"site": str(w.site)})
        ).status_code == 404


async def test_without_a_built_bundle_the_page_is_503(pg, tmp_path) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_ui_static_dir=str(tmp_path)) as w:
        assert (
            await w.client.get(PAGE, params={"site": str(w.site)})
        ).status_code == 503


async def test_only_manifest_assets_are_served(pg, bundle) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_ui_static_dir=str(bundle)) as w:
        ok = await w.client.get(f"{PAGE}/assets/assess-ui.abc123.js")
        assert ok.status_code == 200 and ok.text == "console.log(1);\n"
        assert ok.headers["content-type"].startswith("text/javascript")
        assert ok.headers["x-content-type-options"] == "nosniff"
        for name in (
            "secret.txt",
            "manifest.json",
            "..%2Fsecret.txt",
            "%2E%2E%2Fmanifest.json",
        ):
            assert (await w.client.get(f"{PAGE}/assets/{name}")).status_code == 404, (
                name
            )


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "%2e%2e",
        "%2E%2E%2Fsecret.txt",
        "..%2Fsecret.txt",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "%2Fetc%2Fpasswd",
        "%2F" + "%2F".join(["tmp", "x.js"]),
        "assess-ui.abc123.js%00.css",
    ],
)
async def test_a_traversal_or_absolute_asset_name_is_404(pg, bundle, name: str) -> None:  # type: ignore[no-untyped-def]
    """CodeQL path injection (PR #157): the request names an asset, it never builds a path."""
    async with world(pg, assess_ui_static_dir=str(bundle)) as w:
        r = await w.client.get(f"{PAGE}/assets/{name}")
        assert r.status_code == 404, name
        assert b"not an asset" not in r.content and b"root:" not in r.content


async def test_a_manifest_entry_that_leaves_the_bundle_is_404(pg, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The file served comes from the manifest and must resolve inside the bundle directory:
    a symlink out of it, or a manifest entry with ``..`` or an absolute path, is 404."""
    dist = tmp_path / "dist"
    dist.mkdir()
    outside = tmp_path / "outside.js"
    outside.write_text("stolen\n")
    (dist / "assess-ui.link.js").symlink_to(outside)
    (dist / "assess-ui.ok.css").write_text("body{}\n")
    for js in ("assess-ui.link.js", "../outside.js", str(outside)):
        (dist / "manifest.json").write_text(
            json.dumps({"js": js, "css": "assess-ui.ok.css"})
        )
        from vektra_assess.ui.router import manifest_of

        manifest_of.cache_clear()
        async with world(pg, assess_ui_static_dir=str(dist)) as w:
            name = js.rsplit("/", 1)[-1]
            r = await w.client.get(f"{PAGE}/assets/{name}")
            assert r.status_code == 404, js
            assert b"stolen" not in r.content
            ok = await w.client.get(f"{PAGE}/assets/assess-ui.ok.css")
            assert ok.status_code == 200
    manifest_of.cache_clear()


EXPECTED_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; "
    "object-src 'none'; frame-ancestors {}; "
    "require-trusted-types-for 'script'; trusted-types 'none'"
)


async def test_the_page_headers_byte_for_byte(pg, bundle) -> None:  # type: ignore[no-untyped-def]
    """ARCH-076: exactly the site's Moodle origin may frame the page, no X-Frame-Options."""
    async with world(pg, assess_ui_static_dir=str(bundle)) as w:
        r = await w.client.get(PAGE, params={"site": str(w.site)})
        raw = {k.lower(): v for k, v in r.headers.raw}
        assert (
            raw[b"content-security-policy"]
            == EXPECTED_CSP.format("https://m.example").encode()
        )
        assert raw[b"referrer-policy"] == b"no-referrer"
        assert raw[b"x-content-type-options"] == b"nosniff"
        assert raw[b"cache-control"] == b"no-store"
        assert raw[b"permissions-policy"] == (
            b"clipboard-write=(self), camera=(), microphone=(), geolocation=()"
        )
        assert raw[b"cross-origin-resource-policy"] == b"same-origin"
        assert b"x-frame-options" not in raw
        # another origin for the site: honoured once the short cache entry expires (review #4)
        await pg.run(
            "super",
            "UPDATE assess_sites SET base_url = 'https://other.example:8443' WHERE id = :s",
            {"s": w.site},
        )
        cached = await w.client.get(PAGE, params={"site": str(w.site)})
        assert cached.headers["content-security-policy"] == EXPECTED_CSP.format(
            "https://m.example"
        )
        from vektra_assess.ui.router import SiteOriginCache

        w.app.state.assess_ui_site_cache = SiteOriginCache(ttl_s=0)
        again = await w.client.get(PAGE, params={"site": str(w.site)})
        assert again.headers["content-security-policy"] == EXPECTED_CSP.format(
            "https://other.example:8443"
        )


async def test_an_unknown_site_may_be_framed_by_nobody(pg, bundle) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_ui_static_dir=str(bundle)) as w:
        r = await w.client.get(PAGE, params={"site": str(uuid.uuid4())})
        assert r.headers["content-security-policy"] == EXPECTED_CSP.format("'none'")
        asset = await w.client.get(f"{PAGE}/assets/assess-ui.def456.css")
        assert asset.headers["cross-origin-resource-policy"] == "same-origin"


async def test_the_site_moodle_origin_is_the_framing_origin(pg, bundle) -> None:  # type: ignore[no-untyped-def]
    """Plan 9 task 4: ``assess_sites.moodle_origin`` (revision 0033) is the one origin in
    frame-ancestors and in the page config; it must be a bare origin (CHECK)."""
    from sqlalchemy.exc import DBAPIError

    async with world(pg, assess_ui_static_dir=str(bundle)) as w:
        await pg.run(
            "super",
            "UPDATE assess_sites SET moodle_origin = 'https://moodle.uni.example:8443' "
            "WHERE id = :s",
            {"s": w.site},
        )
        r = await w.client.get(PAGE, params={"site": str(w.site)})
        assert cfg_of(r.text)["moodleOrigin"] == "https://moodle.uni.example:8443"
        assert r.headers["content-security-policy"] == EXPECTED_CSP.format(
            "https://moodle.uni.example:8443"
        )
        for bad in (
            "https://m.example/path",
            "https://*.example",
            "https://u@m.example",
        ):
            with pytest.raises(DBAPIError):
                await pg.run(
                    "super",
                    "UPDATE assess_sites SET moodle_origin = :o WHERE id = :s",
                    {"o": bad, "s": w.site},
                )


# --- review #1: the page is served on VEKTRA_ASSESS_UI_ORIGIN only --------------------------------


async def test_without_a_ui_origin_the_page_is_503(pg, bundle) -> None:  # type: ignore[no-untyped-def]
    from support.world import default_settings

    cfg = default_settings(VEKTRA_ASSESS_UI_ORIGIN="")
    async with world(pg, cfg, assess_ui_static_dir=str(bundle)) as w:
        r = await w.client.get(PAGE, params={"site": str(w.site)})
        assert r.status_code == 503 and r.text == ""
        assert r.headers["content-security-policy"] == EXPECTED_CSP.format("'none'")


@pytest.mark.parametrize(
    "host", ["other.example", "t:8080", "m.example", "evil.example:80"]
)
async def test_the_page_is_503_on_any_other_host(pg, bundle, host: str) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_ui_static_dir=str(bundle)) as w:  # UI origin http://t
        r = await w.client.get(
            PAGE, params={"site": str(w.site)}, headers={"Host": host}
        )
        assert r.status_code == 503
        # the same spelling of the right origin is accepted (case, default port)
        ok = await w.client.get(
            PAGE, params={"site": str(w.site)}, headers={"Host": "T:80"}
        )
        assert ok.status_code == 200


@pytest.mark.parametrize(("trusted", "status"), [("127.0.0.1", 200), ("10.9.9.9", 503)])
async def test_the_forwarded_scheme_counts_only_from_a_trusted_proxy(
    pg, bundle, trusted: str, status: int
) -> None:  # type: ignore[no-untyped-def]
    """The production server (uvicorn) applies X-Forwarded-Proto only for FORWARDED_ALLOW_IPS
    (its ProxyHeadersMiddleware); the page reads the resulting scheme and the Host header."""
    import httpx
    from support.world import default_settings
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    cfg = default_settings(VEKTRA_ASSESS_UI_ORIGIN="https://t")
    async with world(pg, cfg, assess_ui_static_dir=str(bundle)) as w:
        app = ProxyHeadersMiddleware(w.app, trusted_hosts=trusted)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
            base_url="http://t",
        ) as client:
            r = await client.get(
                PAGE,
                params={"site": str(w.site)},
                headers={"X-Forwarded-Proto": "https"},
            )
        assert r.status_code == status


# --- review #4: an unauthenticated page that never floods the database -----------------------------


def _count_checkouts(engine) -> list[int]:  # type: ignore[no-untyped-def]
    from sqlalchemy import event

    seen = [0]

    @event.listens_for(engine.sync_engine, "checkout")
    def _checkout(*_a) -> None:  # type: ignore[no-untyped-def]
        seen[0] += 1

    return seen


async def test_a_repeated_page_request_does_not_touch_the_database(pg, bundle) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_ui_static_dir=str(bundle)) as w:
        checkouts = _count_checkouts(w.engine)
        for _ in range(20):
            r = await w.client.get(PAGE, params={"site": str(w.site)})
            assert r.status_code == 200
        assert checkouts[0] == 1  # the site's origin is cached (short TTL)


async def test_a_flood_of_random_sites_is_rate_limited(pg, bundle) -> None:  # type: ignore[no-untyped-def]
    from support.world import default_settings

    cfg = default_settings(VEKTRA_ASSESS_UI_PAGE_RPM=30)
    async with world(pg, cfg, assess_ui_static_dir=str(bundle)) as w:
        checkouts = _count_checkouts(w.engine)
        codes = [
            (await w.client.get(PAGE, params={"site": str(uuid.uuid4())})).status_code
            for _ in range(200)
        ]
        assert codes[:30] == [404] * 30
        assert set(codes[30:]) == {429}  # the limiter answers before any lookup
        assert (
            checkouts[0] <= 30
        )  # one lookup per allowed request, never one per request
        limited = await w.client.get(PAGE, params={"site": str(uuid.uuid4())})
        assert "retry-after" in {k.lower() for k in limited.headers}
        assert limited.headers["content-security-policy"] == EXPECTED_CSP.format(
            "'none'"
        )
        assert w.engine.pool.checkedout() == 0


async def test_a_site_whose_origin_is_the_ui_origin_is_not_served(pg, bundle) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4501067228: a site re-pointed at the UI origin after startup would let the
    parent and the iframe share an origin (allow-same-origin): the page refuses it."""
    async with world(pg, assess_ui_static_dir=str(bundle)) as w:  # UI origin http://t
        await pg.run(
            "super",
            "UPDATE assess_sites SET moodle_origin = 'http://t' WHERE id = :s",
            {"s": w.site},
        )
        r = await w.client.get(PAGE, params={"site": str(w.site)})
        assert r.status_code == 503
        assert r.headers["content-security-policy"] == EXPECTED_CSP.format("'none'")

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

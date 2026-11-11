"""Live setup check and capability matrix against the seeded Moodle 4.3 / 5.2 dev envs.

``pytest vektra-assess -m moodle`` with the same four variables as ``test_moodle_live`` (URLs,
manifest dir, token dir). The accounts come from ``provision-<tag>.json``: the service account
and the three negative ones (``assess_bad_admin``, ``assess_bad_grader``, ``vektra_ingest_acct``).
The revoked-token case uses a syntactically valid but UNKNOWN token: no token row is deleted
and the live sites stay untouched. Nothing here writes to Moodle.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_moodle_probes as probes_test
from assess_moodle_support import BRANCHES, LiveEnv, Replay, make_client, use_live
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_assess.capabilities import FLAGS, Ready, site_readiness
from vektra_assess.config import VektraAssessSettings
from vektra_assess.moodle import spike
from vektra_assess.moodle.probes import derive_probe_manifest
from vektra_assess.moodle.provider import MoodleLmsProvider
from vektra_assess.moodle.setup_check import SERVICE_ACCOUNT_ITEMS
from vektra_assess.setup import (
    GENERIC_ITEMS,
    PLUGGABLE_ITEMS,
    CheckOutcome,
    ItemRegistry,
    builtin_registry,
    evaluate,
    ok,
)
from vektra_shared.lms import LmsSite

#: the provider's service-account items, then Vektra's own (ADR-0033 D3)
BUILTIN_ITEMS = (*SERVICE_ACCOUNT_ITEMS, *GENERIC_ITEMS)

_ENV = {
    "4.3": ("VEKTRA_ASSESS_MOODLE43_URL", "m43"),
    "5.2": ("VEKTRA_ASSESS_MOODLE52_URL", "m52"),
}
_URL = {b: os.environ.get(var) for b, (var, _tag) in _ENV.items()}
_MANIFESTS = os.environ.get("VEKTRA_ASSESS_MOODLE_MANIFEST_DIR")
_TOKENS = os.environ.get("VEKTRA_ASSESS_MOODLE_TOKEN_DIR")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.moodle,
    pytest.mark.parametrize("branch", BRANCHES),
]

CFG = {
    "VEKTRA_ASSESS_ENABLED": "true",
    "VEKTRA_ASSESS_INGEST_ACCOUNT_USERNAME": "vektra_ingest_acct",
    "VEKTRA_ASSESS_UI_ORIGIN": "https://chat.vektra-ui.test",
    "VEKTRA_ASSESS_API_PUBLIC_ORIGIN": "https://api.vektra-api.test",
}


def _paths(branch: str) -> tuple[Path, Path]:
    var, tag = _ENV[branch]
    if not _URL[branch]:
        pytest.skip(f"BLOCKED: {var} unset")
    if not _MANIFESTS or not _TOKENS:
        pytest.skip("BLOCKED: VEKTRA_ASSESS_MOODLE_MANIFEST_DIR / ..._TOKEN_DIR unset")
    return (
        Path(_MANIFESTS) / f"manifest-{tag}.json",
        Path(_TOKENS) / f"provision-{tag}.json",
    )


def _prov(branch: str) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(_paths(branch)[1].read_text())
    return out


def _env_with(branch: str, token: str) -> LiveEnv:
    manifest = json.loads(_paths(branch)[0].read_text())
    return LiveEnv(str(_URL[branch]), manifest, token)


@pytest.fixture(autouse=True)
def _cleanup(branch: str) -> Iterator[None]:
    yield
    use_live(branch, None)


async def run_check(
    branch: str, token: str, **env: str
) -> tuple[CheckOutcome, dict[str, tuple[str, str | None]]]:
    live = _env_with(branch, token)
    use_live(branch, live)
    client = make_client(Replay(branch))
    cfg_ = VektraAssessSettings(**{**CFG, **env})  # type: ignore[arg-type]
    try:
        out = await evaluate(
            MoodleLmsProvider(client, cfg_),
            LmsSite(f"live-{branch}", live.url),
            cfg_,
            derive_probe_manifest(live.manifest),
        )
    finally:
        await client.aclose()
    return out, {i.name: (i.result, i.reason_code) for i in out.items}


def failing(items: dict[str, tuple[str, str | None]]) -> list[str]:
    return [n for n in BUILTIN_ITEMS if items[n][0] != "OK"]


async def test_the_good_account_passes_every_builtin_item(branch: str) -> None:
    prov = _prov(branch)
    out, items = await run_check(branch, prov["service_account"]["token"])
    print(f"\n[{branch}] release={out.release}")
    for i in out.items:
        print(f"[{branch}] item {i.name}: {i.result} {i.reason_code or ''}")
    assert failing(items) == [], items
    for name in PLUGGABLE_ITEMS:
        assert items[name] == ("MISSING", "not_registered")
    assert items["ws_tls_ok"] == ("OK", "development_plain_http")
    assert not out.token_rejected and out.release == prov["release"]


async def test_all_sixteen_flags_have_the_recorded_state_and_value(branch: str) -> None:
    prov = _prov(branch)
    out, _items = await run_check(branch, prov["service_account"]["token"])
    got = {f.flag: f for f in out.flags}
    assert list(got) == list(FLAGS) and len(got) == 16
    want = {**probes_test.BOTH, **probes_test.EXPECTED_STATE[branch]}
    for flag in FLAGS:
        print(
            f"[{branch}] flag {flag}: {got[flag].state} {got[flag].reason_code or ''}"
        )
        assert (got[flag].state, got[flag].reason_code) == want[flag], flag
        assert got[flag].reason_code != "not_probed"
        assert len(got[flag].evidence_hash) == 64


async def test_bad_admin_fails_not_site_admin(branch: str) -> None:
    token = _prov(branch)["negative"]["assess_bad_admin"]["token"]
    _out, items = await run_check(branch, token)
    print(f"\n[{branch}] bad_admin failing: {failing(items)} {items['not_site_admin']}")
    assert items["not_site_admin"] == ("MISSING", "site_admin")


async def test_bad_grader_fails_no_grade_write(branch: str) -> None:
    token = _prov(branch)["negative"]["assess_bad_grader"]["token"]
    _out, items = await run_check(branch, token)
    print(
        f"\n[{branch}] bad_grader failing: {failing(items)} {items['no_grade_write']}"
    )
    assert items["no_grade_write"] == ("MISSING", "grade_write_function_in_service")


async def test_the_ingestion_token_fails_not_ingestion_account(branch: str) -> None:
    token = _prov(branch)["negative"]["vektra_ingest_acct"]["token"]
    _out, items = await run_check(branch, token)
    print(
        f"\n[{branch}] ingest failing: {failing(items)} {items['not_ingestion_account']}"
    )
    assert items["not_ingestion_account"] == ("MISSING", "is_ingestion_account")


async def test_the_ingestion_item_is_unverifiable_without_the_setting(
    branch: str,
) -> None:
    token = _prov(branch)["service_account"]["token"]
    live = _env_with(branch, token)
    use_live(branch, live)
    client = make_client(Replay(branch))
    cfg = {k: v for k, v in CFG.items() if "INGEST" not in k}
    cfg_ = VektraAssessSettings(**cfg)  # type: ignore[arg-type]
    try:
        out = await evaluate(
            MoodleLmsProvider(client, cfg_),
            LmsSite(f"live-{branch}", live.url),
            cfg_,
            derive_probe_manifest(live.manifest),
            probe_flags=False,
        )
    finally:
        await client.aclose()
    items = {i.name: (i.result, i.reason_code) for i in out.items}
    assert items["not_ingestion_account"] == ("UNVERIFIABLE", "ingest_username_unset")


async def test_an_unknown_token_reads_as_revoked(branch: str) -> None:
    """A token Moodle does not know answers ``invalidtoken``: the same signal as a deleted one."""
    out, items = await run_check(branch, "0123456789abcdef0123456789abcdef")
    print(
        f"\n[{branch}] unknown token: {items['token_works']} rejected={out.token_rejected}"
    )
    assert items["token_works"] == ("MISSING", "invalidtoken")
    assert out.token_rejected and out.token_reason == "invalidtoken"
    assert out.flags == () and out.release is None
    assert items["not_site_admin"] == ("MISSING", "token_works_failed")


def _cli(branch: str, *extra: str) -> list[str]:
    manifest, prov = _paths(branch)
    return [
        "--site", str(_URL[branch]),
        "--manifest", str(manifest),
        "--token-file", str(prov),
        "--allow-http", "--allow-cidr", "127.0.0.0/8", "--resolve-ipv4",
        "--ingest-account", "vektra_ingest_acct",
        "--ui-origin", "https://chat.vektra-ui.test",
        "--api-origin", "https://api.vektra-api.test",
        *extra,
    ]  # fmt: skip


def test_the_spike_cli_prints_the_matrix_and_the_items_content_free(
    branch: str, capsys: pytest.CaptureFixture[str]
) -> None:
    prov = _prov(branch)
    rc = spike.main(_cli(branch, "--no-db", "--json"))
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    doc = json.loads(captured.out)
    assert [c["flag"] for c in doc["capabilities"]] == list(FLAGS)
    assert "not_probed" not in {c["reason_code"] for c in doc["capabilities"]}
    assert doc["release"] == prov["release"]
    assert {i["name"] for i in doc["items"]} == {*BUILTIN_ITEMS, *PLUGGABLE_ITEMS}
    everything = captured.out + captured.err
    for secret in (
        prov["service_account"]["token"],
        str(_URL[branch]),
        "localhost",
        "CANARY",
    ):
        assert secret not in everything
    # the table form too
    rc = spike.main(_cli(branch, "--no-db"))
    table = capsys.readouterr().out
    assert rc == 0 and "CAPABILITY MATRIX" in table
    assert all(f in table for f in FLAGS)
    assert prov["service_account"]["token"] not in table


def test_the_spike_cli_persists_through_the_worker_code_path(
    branch: str, pg: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """With a worker DSN the CLI runs ``run_setup_check``: matrix, report and audit rows."""
    site = uuid.uuid4()
    url = str(_URL[branch])

    async def seed() -> None:
        await pg.run(
            "super",
            "INSERT INTO assess_sites (id, moodle_site_uuid, base_url) VALUES (:i, :u, :b)",
            {"i": site, "u": str(site), "b": url},
        )

    asyncio.run(seed())
    rc = spike.main(_cli(branch, "--db-url", pg.url("worker"), "--json"))
    out = capsys.readouterr()
    assert rc == 0, out.err

    async def verify() -> None:
        rows = await pg.run(
            "super",
            "SELECT flag, state, release FROM assess_site_capabilities WHERE site_id=:s",
            {"s": site},
        )
        assert len(rows) == len(FLAGS)
        rep = await pg.run(
            "super",
            "SELECT status, moodle_release FROM assess_setup_check_reports WHERE site_id=:s",
            {"s": site},
        )
        assert [r[0] for r in rep] == ["done"]
        stored = await pg.run(
            "super",
            "SELECT probe_manifest IS NOT NULL FROM assess_sites WHERE id=:s",
            {"s": site},
        )
        assert stored[0][0] is True
        engine = create_async_engine(pg.url("worker"))
        try:
            async with engine.connect() as c:
                ready = await site_readiness(c, site)
                # three pluggable items are not registered yet: NotReady, by name
                assert getattr(ready, "items", ()) == PLUGGABLE_ITEMS
                assert ready != Ready()
        finally:
            await engine.dispose()

    asyncio.run(verify())
    # an unregistered site is a usage error, not a traceback
    rc = spike.main(
        [*_cli(branch, "--db-url", pg.url("worker")), "--site", "http://localhost:1"]
    )
    assert rc == 2


def test_registry_has_no_registered_pluggable_item_by_default(branch: str) -> None:
    reg: ItemRegistry = builtin_registry()
    assert not set(PLUGGABLE_ITEMS) & set(reg.names())
    assert ok().result == "OK"

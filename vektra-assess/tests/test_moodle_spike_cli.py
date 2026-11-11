"""The spike CLI, hermetically: the recorded responses stand in for the dev site."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from assess_moodle_support import (
    BASE_URL,
    FIXTURES,
    TOKEN,
    Replay,
    make_client,
    manifest,
)

from vektra_assess.capabilities import FLAGS
from vektra_assess.moodle import spike
from vektra_assess.moodle.setup_check import SERVICE_ACCOUNT_ITEMS
from vektra_assess.setup import GENERIC_ITEMS, PLUGGABLE_ITEMS

BUILTIN_ITEMS = (*SERVICE_ACCOUNT_ITEMS, *GENERIC_ITEMS)


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    seed = tmp_path / "manifest.json"
    seed.write_text((FIXTURES / "5.2" / "manifest.json").read_text())
    prov = tmp_path / "provision.json"
    prov.write_text(json.dumps({"service_account": {"token": TOKEN}}))
    raw = tmp_path / "token.txt"
    raw.write_text(TOKEN + "\n")
    return {"seed": seed, "prov": prov, "raw": raw}


@pytest.fixture
def replay_site(monkeypatch: pytest.MonkeyPatch) -> Replay:
    replay = Replay("5.2")
    monkeypatch.setattr(spike, "MoodleWsClient", lambda *_a, **_k: make_client(replay))
    return replay


def args(files: dict[str, Path], *extra: str) -> list[str]:
    return [
        "--site", BASE_URL, "--manifest", str(files["seed"]),
        "--token-file", str(files["prov"]), "--no-db",
        "--ingest-account", "vektra_ingest_acct",
        "--ui-origin", "https://chat.vektra-ui.test",
        "--api-origin", "https://api.vektra-api.test",
        *extra,
    ]  # fmt: skip


def test_json_output_is_content_free(
    files: dict[str, Path], replay_site: Replay, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = spike.main(args(files, "--json"))
    out = capsys.readouterr()
    assert rc == 0, out.err
    doc = json.loads(out.out)
    assert [c["flag"] for c in doc["capabilities"]] == list(FLAGS)
    assert {i["name"] for i in doc["items"]} == {*BUILTIN_ITEMS, *PLUGGABLE_ITEMS}
    assert doc["release"] == manifest("5.2")["release"]
    assert set(doc) == {"release", "items", "capabilities"}
    everything = out.out + out.err
    for needle in (TOKEN, "moodle.example.org", "CANARY", "aas_"):
        assert needle not in everything
    assert not replay_site.unmatched


def test_table_output_and_a_raw_token_file(
    files: dict[str, Path], replay_site: Replay, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = args(files)
    argv[argv.index("--token-file") + 1] = str(files["raw"])
    assert spike.main(argv) == 0
    table = capsys.readouterr().out
    assert "ITEMS" in table and "CAPABILITY MATRIX" in table
    assert all(name in table for name in (*BUILTIN_ITEMS, *FLAGS))
    row = next(line for line in table.splitlines() if "parser_sidecar_running" in line)
    assert row.split() == ["parser_sidecar_running", "MISSING", "not_registered"]


def test_exit_status_when_a_flag_is_not_probed(
    files: dict[str, Path], replay_site: Replay, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = args(files)
    del argv[
        argv.index("--manifest") : argv.index("--manifest") + 2
    ]  # no probe manifest
    assert spike.main(argv) == 1
    assert "not_probed" in capsys.readouterr().out


def test_usage_errors_are_exit_2_without_a_traceback(
    files: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    argv = args(files)
    no_token = [a for a in argv if a not in ("--token-file", str(files["prov"]))]
    assert spike.main(no_token) == 2
    assert "--token-file" in capsys.readouterr().err
    missing = args(files)
    missing[missing.index("--manifest") + 1] = str(files["seed"]) + ".nope"
    assert spike.main(missing) == 2
    assert "cannot use the input files" in capsys.readouterr().err
    files["prov"].write_text(json.dumps({"service_account": {}}))
    assert spike.main(args(files)) == 2
    assert TOKEN not in capsys.readouterr().err


def test_read_token_shapes(files: dict[str, Path]) -> None:
    assert spike.read_token(files["prov"], "service_account.token") == TOKEN
    assert spike.read_token(files["raw"], "service_account.token") == TOKEN
    with pytest.raises(spike.UsageError):
        spike.read_token(files["prov"], "service_account.missing")


def test_settings_overrides_and_ipv4_resolver(
    files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    ns = spike.parser().parse_args(
        [
            *args(files),
            "--allow-http",
            "--allow-cidr",
            "10.0.0.0/8",
            "--allow-cidr",
            "::1/128",
        ]
    )
    s = spike.build_settings(ns)
    assert s.moodle_allow_http is True
    assert s.moodle_allowed_cidrs == "10.0.0.0/8,::1/128"
    assert s.ingest_account_username == "vektra_ingest_acct"
    assert s.ui_origin == "https://chat.vektra-ui.test"

    both: list[Any] = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 80, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: list(both))
    assert [r[4][0] for r in spike.ipv4_resolver("localhost", 80)] == ["127.0.0.1"]
    only6 = both[:1]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: list(only6))
    assert spike.ipv4_resolver("localhost", 80) == only6

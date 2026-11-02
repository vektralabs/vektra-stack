"""The recorder refuses anything that is not a local DEV site and scrubs what it records."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "assess_record_ws",
    Path(__file__).resolve().parents[2] / "scripts" / "assess_record_ws.py",
)
assert _SPEC and _SPEC.loader
rec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rec)

TOKEN = "f" * 32


@pytest.mark.parametrize(
    "url",
    [
        "https://moodle.example.org",
        "http://moodle.example.org:8080",
        "http://localhost",  # no port
        "http://localhost:8080.evil.example",
        "http://evil.example/localhost:8080",
        "http://10.0.0.5:8080",
        "https://localhost:8443",
        "http://user@localhost:8080",
        "",
    ],
)
def test_refuses_a_base_url_that_is_not_local_http(url: str) -> None:
    with pytest.raises(rec.RecorderRefused):
        rec.check_base_url(url)


@pytest.mark.parametrize("url", ["http://localhost:8080", "http://127.0.0.1:8082/"])
def test_accepts_local_dev_urls(url: str) -> None:
    assert rec.check_base_url(url).startswith("http://")


@pytest.mark.parametrize(
    "release",
    [
        "4.3.11 (Build: 20250411)",
        "4.4.1+ (Build: 20250101)",
        "5.1.2+ (Build: 1)",
        "5.2.4+",
        None,
        5,
    ],
)
def test_refuses_a_release_that_is_not_a_43_or_52_dev_build(release: Any) -> None:
    with pytest.raises(rec.RecorderRefused):
        rec.check_release(release)


def test_accepts_the_dev_builds() -> None:
    assert rec.check_release("4.3.11+ (Build: 20250411)") == "4.3"
    assert rec.check_release("5.2.4+ (Build: 20261030)") == "5.2"


def test_scrub_removes_host_token_key_and_token_urls() -> None:
    base = "http://localhost:8080"
    doc = {
        "siteurl": base,
        "userprivateaccesskey": "abcdef",
        "html": r'{"url":"http:\/\/localhost:8080\/mod\/quiz\/review.php"}',
        "fileurl": f"{base}/webservice/pluginfile.php/1/a/b/2/f.txt?token={TOKEN}&x=1",
        "note": f"the token {TOKEN} must go",
        "nested": [{"u": "http://127.0.0.1:9999/x"}],
    }
    out = rec.scrub(doc, base_url=base, token=TOKEN)
    blob = json.dumps(out)
    rec.assert_clean(blob, base_url=base, token=TOKEN)
    assert out["userprivateaccesskey"] == "REDACTED"
    assert out["siteurl"] == rec.SCRUBBED_ROOT
    assert "token=REDACTED" in out["fileurl"] and TOKEN not in blob
    assert "localhost" not in blob and "127.0.0.1" not in blob


def test_assert_clean_refuses_a_surviving_secret() -> None:
    for blob in (
        f"x {TOKEN} y",
        "see http://localhost:8080/a",
        '"userprivateaccesskey": "abc"',
    ):
        with pytest.raises(rec.RecorderRefused):
            rec.assert_clean(blob, base_url="http://localhost:8080", token=TOKEN)


def test_record_refuses_a_site_whose_release_does_not_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        rec, "ws_call", lambda *a, **k: {"release": "4.5.0+ (Build: 1)"}
    )
    with pytest.raises(rec.RecorderRefused):
        rec.record(
            "http://localhost:8080", TOKEN, {"release": "4.5.0+ (Build: 1)"}, tmp_path
        )
    assert list(tmp_path.iterdir()) == []  # nothing written


def test_record_refuses_a_manifest_of_another_site(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        rec, "ws_call", lambda *a, **k: {"release": "4.3.11+ (Build: 20250411)"}
    )
    with pytest.raises(rec.RecorderRefused):
        rec.record(
            "http://localhost:8080", TOKEN, {"release": "5.2.4+ (Build: 1)"}, tmp_path
        )


def test_record_refuses_a_non_local_url_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no request may be sent to a non-local host")

    monkeypatch.setattr(rec, "ws_call", boom)
    with pytest.raises(rec.RecorderRefused):
        rec.record("https://moodle.uni.example", TOKEN, {}, tmp_path)


def test_main_exit_codes(tmp_path: Path) -> None:
    tok = tmp_path / "p.json"
    tok.write_text(json.dumps({"service_account": {"token": TOKEN}}))
    man = tmp_path / "m.json"
    man.write_text("{}")
    assert (
        rec.main(
            [
                "--base-url",
                "https://x.example",
                "--token-file",
                str(tok),
                "--manifest",
                str(man),
            ]
        )
        == 2
    )

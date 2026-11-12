"""The recorder refuses anything that is not a local DEV site and scrubs what it records."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "assess_record_ws",
    Path(__file__).resolve().parents[1] / "scripts" / "record_ws.py",
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
        "4.4.1+ (Build: 20250101)",
        "5.1.2+ (Build: 1)",
        "5.3.0 (Build: 20261005)",
        "5.2.4+",
        "5.2.4",
        "5.2.4++ (Build: 20261005)",
        "5.2.4 (Build: 20261005) ",
        "5.2.4  (Build: 20261005)",
        None,
        5,
    ],
)
def test_refuses_a_release_that_is_not_a_43_or_52_dev_build(release: Any) -> None:
    with pytest.raises(rec.RecorderRefused):
        rec.check_release(release)


@pytest.mark.parametrize(
    ("release", "branch"),
    [
        # a weekly build carries the "+"
        ("4.3.11+ (Build: 20250411)", "4.3"),
        ("5.2.4+ (Build: 20261030)", "5.2"),
        # a site pinned to a release tag (vektra-moodle#43: 5.2 is on 5.2.4) has no "+"
        ("5.2.4 (Build: 20261005)", "5.2"),
        ("4.3.11 (Build: 20250411)", "4.3"),
    ],
)
def test_accepts_the_dev_builds(release: str, branch: str) -> None:
    assert rec.check_release(release) == branch


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


def test_record_refuses_a_contaminated_download_by_position_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CodeRabbit 4499897062: the refusal names the file's position, never its name."""
    secret_name = (
        f"leak-{TOKEN}.txt"  # a name is unchecked: it must not reach the message
    )
    files = {
        "clean.txt": b"VISIBLE text",
        secret_name: b"see http://localhost:8082/x",  # the 2nd of 3 carries the dev host
        "late.txt": b"more VISIBLE text",
    }
    _patch_site(monkeypatch, files)
    with pytest.raises(rec.RecorderRefused) as refused:
        rec.record(DEV, TOKEN, _manifest(), tmp_path)
    message = str(refused.value)
    assert "file 2 of 3" in message
    assert TOKEN not in message and "leak" not in message
    assert list(tmp_path.iterdir()) == []  # nothing written


DEV = "http://localhost:8082"
_INFO = {
    "release": "5.2.4 (Build: 20261005)",
    "version": "2026042004",
    "siteurl": DEV,
    "username": "vektra_assess_svc",
    "userid": 3,
}


def _manifest() -> dict[str, Any]:
    return {
        "release": _INFO["release"],
        "version": _INFO["version"],
        "courseid": 2,
        "users": {
            "stu1": {"id": 7, "role": "student"},
            "grader1": {"id": 12, "role": "editingteacher"},
        },
        "assigns": {"files": {"id": 9, "submissions": {"stu1": {}}}},
    }


def _patch_site(
    monkeypatch: pytest.MonkeyPatch, files: dict[str, bytes], **override: Any
) -> None:
    """A fake seeded dev site; ``override`` replaces one wsfunction's answer."""
    answers: dict[str, Any] = {
        "core_webservice_get_site_info": _INFO,
        "core_enrol_get_users_courses": [{"id": 2, "shortname": "EAASSESS"}],
        "core_enrol_get_enrolled_users": [{"id": 7}, {"id": 12}, {"id": 99}],
        **override,
    }

    def ws_call(_base: str, _token: str, fn: str, _params: Any) -> Any:
        if fn in answers:
            return answers[fn]
        if fn == "mod_assign_get_submission_status":
            area = {"files": [{"filename": n, "fileurl": f"/f/{n}"} for n in files]}
            return {"lastattempt": {"submission": {"plugins": [{"fileareas": [area]}]}}}
        return {}

    monkeypatch.setattr(rec, "ws_call", ws_call)
    monkeypatch.setattr(rec, "plan", lambda *_a: [])
    monkeypatch.setattr(rec, "download", lambda _b, _t, url: files[url.split("/")[-1]])


def test_record_accepts_a_seeded_dev_site(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_site(monkeypatch, {"clean.txt": b"VISIBLE text"})
    assert rec.record(DEV, TOKEN, _manifest(), tmp_path)["files"] == 1
    assert (tmp_path / "5.2" / "files" / "stu1__clean.txt").exists()


@pytest.mark.parametrize(
    "override",
    [
        # a production site forwarded to localhost reports its real wwwroot
        {
            "core_webservice_get_site_info": {
                **_INFO,
                "siteurl": "https://moodle.uni.example",
            }
        },
        {
            "core_webservice_get_site_info": {
                **_INFO,
                "siteurl": "http://localhost:8080",
            }
        },
        {
            "core_webservice_get_site_info": {
                k: v for k, v in _INFO.items() if k != "siteurl"
            }
        },
        {"core_webservice_get_site_info": {**_INFO, "username": "ws_prod"}},
        {"core_webservice_get_site_info": {**_INFO, "version": "2026042099"}},
        {"core_enrol_get_users_courses": [{"id": 2, "shortname": "MATH101"}]},
        {"core_enrol_get_users_courses": [{"id": 3, "shortname": "EAASSESS"}]},
        {"core_enrol_get_users_courses": {"exception": "x"}},
        {"core_enrol_get_enrolled_users": [{"id": 7}]},  # grader1 missing
        {"core_enrol_get_enrolled_users": {"exception": "x"}},
    ],
    ids=[
        "wwwroot_production",
        "wwwroot_other_dev_port",
        "wwwroot_missing",
        "not_the_service_account",
        "other_version",
        "course_shortname",
        "course_id",
        "courses_error",
        "seed_user_missing",
        "enrolment_error",
    ],
)
def test_record_refuses_a_matching_release_with_another_identity(
    override: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CodeRabbit 4500701647: a pinned release string is not an identity."""
    _patch_site(monkeypatch, {"clean.txt": b"VISIBLE text"}, **override)
    with pytest.raises(rec.RecorderRefused):
        rec.record(DEV, TOKEN, _manifest(), tmp_path)
    assert list(tmp_path.iterdir()) == []  # nothing written


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

"""REQ-079 setup check against the recorded 4.3 and 5.2 responses (plan group E).

The negative accounts are the recorded good response with exactly the field that makes the
account bad (``userissiteadmin``, a grade-write function, the ingestion username, ``auth``);
the live suite uses the real accounts. A request with no recording fails the test.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from assess_moodle_support import (
    BRANCHES,
    FIXTURES,
    TOKEN,
    Replay,
    make_client,
    manifest,
    now,
    resolver,
    site_for,
)

from vektra_assess.capabilities import FLAGS
from vektra_assess.config import VektraAssessSettings
from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.probes import derive_probe_manifest
from vektra_assess.moodle.provider import MoodleLmsProvider
from vektra_assess.moodle.readers import read_own_auth, read_site_info
from vektra_assess.moodle.result import Readable, Unreadable, UnreadableReason
from vektra_assess.moodle.setup_check import (
    GRADE_WRITE_FUNCTIONS,
    SERVICE_ACCOUNT_ITEMS,
    is_grade_write_function,
)
from vektra_assess.setup import (
    DEFAULT_REGISTRY,
    GENERIC_ITEMS,
    PLUGGABLE_ITEMS,
    CheckOutcome,
    ItemRegistry,
    Outcome,
    evaluate,
    ok,
    same_site,
)
from vektra_shared.lms import LmsSite

pytestmark = pytest.mark.parametrize("branch", BRANCHES)

#: the provider's service-account items, then Vektra's own built-in items (ADR-0033 D3)
BUILTIN_ITEMS = (*SERVICE_ACCOUNT_ITEMS, *GENERIC_ITEMS)

SITE_INFO = "core_webservice_get_site_info"
ENV = {
    "VEKTRA_ASSESS_ENABLED": "true",
    "VEKTRA_ASSESS_INGEST_ACCOUNT_USERNAME": "vektra_ingest_acct",
    "VEKTRA_ASSESS_UI_ORIGIN": "https://chat.vektra-ui.test",
    "VEKTRA_ASSESS_API_PUBLIC_ORIGIN": "https://api.vektra-api.test",
}


def settings(**env: str) -> VektraAssessSettings:
    return VektraAssessSettings(**{**ENV, **env})  # type: ignore[arg-type]


def serve_files(replay: Replay, branch: str) -> None:
    """Serve the recorded submission files at the URLs the recorded status lists."""
    status = json.loads(
        (
            FIXTURES / branch / "mod_assign_get_submission_status__files-stu1.json"
        ).read_text()
    )["response"]
    for plugin in status["lastattempt"]["submission"]["plugins"]:
        for area in plugin.get("fileareas", []):
            for f in area["files"]:
                local = FIXTURES / branch / "files" / f"stu1__{f['filename']}"
                if local.exists():
                    replay.files[urlsplit(f["fileurl"]).path] = local.read_bytes()


def provider(client: MoodleWsClient, branch: str) -> MoodleLmsProvider:
    """The Moodle provider over ``client``, with the branch's recorded clock for the probes."""
    return MoodleLmsProvider(client, client._s, clock=lambda: now(branch))


def lms(branch: str) -> LmsSite:
    return LmsSite("site-1", site_for(branch).base_url)


def patch(**by_function: Callable[[Any], Any]) -> Callable[[str, Any], Any]:
    def mutate(fn: str, body: Any) -> Any:
        return by_function[fn](body) if fn in by_function else body

    return mutate


async def good(
    branch: str, **kw: Any
) -> tuple[Replay, dict[str, tuple[str, str | None]], CheckOutcome]:
    replay = Replay(branch)
    serve_files(replay, branch)
    replay.mutate = kw.pop("mutate", None)
    cfg = kw.pop("cfg", None) or settings()
    client = kw.pop("client", None) or make_client(replay)
    out = await evaluate(
        MoodleLmsProvider(client, cfg, clock=lambda: now(branch)),
        lms(branch),
        cfg,
        kw.pop("pm", None) or derive_probe_manifest(manifest(branch)),
        registry=kw.pop("registry", None),
        **({"lms_origin": kw.pop("lms_origin")} if "lms_origin" in kw else {}),
    )
    assert not replay.unmatched
    return replay, {i.name: (i.result, i.reason_code) for i in out.items}, out


async def test_every_builtin_item_is_ok_on_the_good_account(branch: str) -> None:
    replay, items, out = await good(branch)
    assert list(items) == [*BUILTIN_ITEMS, *PLUGGABLE_ITEMS]
    for name in BUILTIN_ITEMS:
        assert items[name][0] == "OK", (name, items[name])
    # an unregistered pluggable item is MISSING: the check never fakes OK
    for name in PLUGGABLE_ITEMS:
        assert items[name] == ("MISSING", "not_registered")
    assert items["ws_tls_ok"] == ("OK", None)  # https
    assert out.release == manifest(branch)["release"]
    assert not out.token_rejected and not out.all_ok
    assert [f.flag for f in out.flags] == list(FLAGS)
    # the denied probe is the only call to a function outside the service
    assert [fn for fn, _ in replay.calls].count("core_course_get_categories") == 1


async def test_a_registered_pluggable_item_is_reported(branch: str) -> None:
    reg = ItemRegistry()
    for name in GENERIC_ITEMS:
        c = DEFAULT_REGISTRY.get(name)
        assert c is not None
        reg.register(name, c)

    async def fine(_ctx: Any) -> Outcome:
        return ok()

    async def boom(_ctx: Any) -> Outcome:
        raise RuntimeError("secret detail")

    reg.register("parser_sidecar_running", fine)
    reg.register("smtp_probe", boom)
    reg.register("extra_item", fine)
    _r, items, _o = await good(branch, registry=reg)
    assert items["parser_sidecar_running"] == ("OK", None)
    assert items["smtp_probe"] == ("UNVERIFIABLE", "check_error")  # an error is not OK
    assert items["db_log_statement_none"] == ("MISSING", "not_registered")
    assert items["extra_item"] == ("OK", None)
    with pytest.raises(ValueError):
        reg.register("extra_item", fine)
    with pytest.raises(ValueError):
        reg.register("Bad Name", fine)


async def test_bad_admin_fails_not_site_admin_by_name(branch: str) -> None:
    mutate = patch(**{SITE_INFO: lambda b: {**b, "userissiteadmin": True}})
    _r, items, _o = await good(branch, mutate=mutate)
    assert items["not_site_admin"] == ("MISSING", "site_admin")
    assert [n for n in BUILTIN_ITEMS if items[n][0] != "OK"] == ["not_site_admin"]


async def test_bad_grader_fails_no_grade_write_by_name(branch: str) -> None:
    def add(b: Any) -> Any:
        return {
            **b,
            "functions": [
                *b["functions"],
                {"name": "mod_assign_save_grade", "version": "2023100900"},
            ],
        }

    _r, items, _o = await good(branch, mutate=patch(**{SITE_INFO: add}))
    assert items["no_grade_write"] == ("MISSING", "grade_write_function_in_service")
    assert [n for n in BUILTIN_ITEMS if items[n][0] != "OK"] == ["no_grade_write"]


async def test_the_ingestion_account_fails_by_name_and_unset_is_unverifiable(
    branch: str,
) -> None:
    def as_ingest(b: Any) -> Any:
        return {**b, "username": "vektra_ingest_acct"}

    _r, items, _o = await good(branch, mutate=patch(**{SITE_INFO: as_ingest}))
    assert items["not_ingestion_account"] == ("MISSING", "is_ingestion_account")
    assert [n for n in BUILTIN_ITEMS if items[n][0] != "OK"] == [
        "not_ingestion_account"
    ]
    # compared without case
    _r, items, _o = await good(
        branch,
        mutate=patch(**{SITE_INFO: lambda b: {**b, "username": "VEKTRA_Ingest_Acct"}}),
    )
    assert items["not_ingestion_account"][0] == "MISSING"
    # setting unset: not verifiable, and that is not OK
    unset = VektraAssessSettings(**{k: v for k, v in ENV.items() if "INGEST" not in k})  # type: ignore[arg-type]
    _r, items, _o = await good(branch, cfg=unset)
    assert items["not_ingestion_account"] == ("UNVERIFIABLE", "ingest_username_unset")


async def test_interactive_login_needs_auth_webservice(branch: str) -> None:
    def manual(b: Any) -> Any:
        return [{**u, "auth": "manual"} for u in b]

    _r, items, _o = await good(
        branch, mutate=patch(core_user_get_users_by_field=manual)
    )
    assert items["interactive_login_disabled"] == ("MISSING", "auth_manual")

    # somebody else's record is not an answer
    def other(b: Any) -> Any:
        return [{**u, "id": u["id"] + 1} for u in b]

    _r, items, _o = await good(branch, mutate=patch(core_user_get_users_by_field=other))
    assert items["interactive_login_disabled"] == ("UNVERIFIABLE", "partial")


async def test_the_denied_function_must_be_refused(branch: str) -> None:
    _r, items, _o = await good(
        branch, mutate=patch(core_course_get_categories=lambda _b: [])
    )
    assert items["denied_function_probe"] == ("MISSING", "denied_function_succeeded")
    # a different failure is not a refusal either
    _r, items, _o = await good(
        branch,
        mutate=patch(
            core_course_get_categories=lambda _b: {
                "exception": "moodle_exception",
                "errorcode": "somethingelse",
                "message": "x",
            }
        ),
    )
    assert items["denied_function_probe"][0] == "UNVERIFIABLE"


async def test_a_reader_group_that_breaks_is_named(branch: str) -> None:
    _r, items, _o = await good(
        branch,
        mutate=patch(
            core_enrol_get_enrolled_users_with_capability=lambda _b: {
                "exception": "webservice_access_exception",
                "errorcode": "nopermissions",
                "message": "x",
            }
        ),
    )
    assert items["required_reads_enrol"] == ("MISSING", "unreadable_access_exception")
    assert [n for n in BUILTIN_ITEMS if items[n][0] != "OK"] == ["required_reads_enrol"]


async def test_without_a_probe_manifest_the_reads_are_unverifiable(branch: str) -> None:
    replay = Replay(branch)
    out = await evaluate(
        provider(make_client(replay), branch),
        lms(branch),
        settings(),
        None,
    )
    items = {i.name: (i.result, i.reason_code) for i in out.items}
    for g in ("quiz", "assign", "grade", "grading", "enrol", "files"):
        assert items[f"required_reads_{g}"] == ("UNVERIFIABLE", "no_probe_manifest")
    assert {f.reason_code for f in out.flags} == {"not_probed"}


async def test_a_revoked_token_fails_token_works_and_stops_the_calls(
    branch: str,
) -> None:
    envelope = {
        "exception": "moodle_exception",
        "errorcode": "invalidtoken",
        "message": "x",
    }
    replay = Replay(branch)
    replay.mutate = lambda _fn, _body: envelope
    out = await evaluate(
        provider(make_client(replay), branch),
        lms(branch),
        settings(),
        derive_probe_manifest(manifest(branch)),
    )
    items = {i.name: (i.result, i.reason_code) for i in out.items}
    assert items["token_works"] == ("MISSING", "invalidtoken")
    assert out.token_rejected and out.token_reason == "invalidtoken"
    assert out.release is None and out.flags == ()
    # nothing but the one site_info call went to Moodle
    assert [fn for fn, _ in replay.calls] == [SITE_INFO]
    for name in ("interactive_login_disabled", "not_site_admin", "required_reads_quiz"):
        assert items[name] == ("MISSING", "token_works_failed")


async def test_no_token_is_not_a_rejection(branch: str) -> None:
    replay = Replay(branch)
    client = MoodleWsClient(
        VektraAssessSettings(VEKTRA_ASSESS_ENABLED="true"),  # type: ignore[arg-type]
        token_provider=lambda _s: None,
        transport=httpx.MockTransport(replay.handler),
        resolver=resolver(),
    )
    out = await evaluate(
        provider(client, branch),
        lms(branch),
        settings(),
        None,
    )
    items = {i.name: (i.result, i.reason_code) for i in out.items}
    assert items["token_works"] == ("MISSING", "no_token")
    assert (
        out.token_rejected is False
    )  # a token we never had was not "rejected by Moodle"
    assert replay.calls == []


async def test_a_slow_or_broken_site_is_unverifiable_not_revoked(branch: str) -> None:
    client = MoodleWsClient(
        VektraAssessSettings(VEKTRA_ASSESS_ENABLED="true"),  # type: ignore[arg-type]
        token_provider=lambda _s: TOKEN,
        transport=httpx.MockTransport(lambda _r: httpx.Response(503)),
        resolver=resolver(),
    )
    out = await evaluate(
        provider(client, branch),
        lms(branch),
        settings(),
        None,
    )
    items = {i.name: (i.result, i.reason_code) for i in out.items}
    assert items["token_works"][0] == "UNVERIFIABLE"
    assert items["not_site_admin"] == ("UNVERIFIABLE", "site_unreachable")
    assert not out.token_rejected


async def test_the_report_is_content_free(branch: str) -> None:
    _r, _items, out = await good(branch)
    blob = json.dumps([i.as_json() for i in out.items]) + repr(out.flags)
    m = manifest(branch)
    for canary in m["canaries"]:
        assert canary not in blob
    for needle in (TOKEN, "moodle.example.org", "aas_", "userid"):
        assert needle not in blob
    assert all(set(i.as_json()) == {"name", "result", "reason_code"} for i in out.items)
    assert len(blob) < 8000  # the report row is capped at 16 kB


async def test_the_files_group_needs_the_download(branch: str) -> None:
    replay = Replay(branch)  # no files served: the download is a 404
    out = await evaluate(
        provider(make_client(replay), branch),
        lms(branch),
        settings(),
        derive_probe_manifest(manifest(branch)),
    )
    items = {i.name: (i.result, i.reason_code) for i in out.items}
    assert items["required_reads_files"][0] == "MISSING"

    def no_download(b: Any) -> Any:
        return {**b, "downloadfiles": 0}

    _r, items, _o = await good(branch, mutate=patch(**{SITE_INFO: no_download}))
    assert items["required_reads_files"] == ("MISSING", "downloadfiles_disabled")


async def test_read_own_auth_reader(branch: str) -> None:
    replay = Replay(branch)
    client = make_client(replay)
    site = site_for(branch)
    info = await read_site_info(client, SiteRef("s", site.base_url))
    assert isinstance(info, Readable)
    got = await read_own_auth(client, site, info.value.userid)
    assert isinstance(got, Readable) and got.value == "webservice"
    replay.mutate = patch(
        core_user_get_users_by_field=lambda b: [{**u, "id": u["id"] + 1} for u in b]
    )
    other = await read_own_auth(client, site, info.value.userid)
    assert isinstance(other, Unreadable) and other.reason is UnreadableReason.PARTIAL


# --- same-site, denylist (pure) ----------------------------------------------------------------------


def test_same_site_rules(branch: str) -> None:
    assert same_site("https://moodle.example.org", "https://moodle.example.org:8443")
    assert same_site("https://chat.example.org", "https://example.org")  # subdomain
    assert same_site(
        "https://chat.example.org", "https://moodle.example.org"
    )  # siblings
    assert same_site("http://localhost:3000", "http://localhost:8080")
    assert same_site(
        "https://a.example.co.uk", "https://b.example.co.uk"
    )  # no PSL: fails closed
    assert not same_site("https://chat.vektra-ui.test", "https://moodle.example.org")
    assert not same_site(
        "http://10.0.0.1", "http://10.0.0.2"
    )  # IP literals: equal only
    assert same_site("http://10.0.0.1:81", "http://10.0.0.1:82")
    assert same_site("https://EXAMPLE.org.", "https://example.org")
    with pytest.raises(ValueError):
        same_site("ftp://example.org", "https://example.org")


async def test_ui_origin_item(branch: str) -> None:
    cases = {
        ("https://moodle.example.org", "https://api.vektra-api.test"): (
            "MISSING",
            "same_site_as_lms",
        ),
        ("https://chat.vektra-ui.test", "https://api.vektra-api.test"): (
            "MISSING",
            "same_site_as_api",
        ),
        ("https://chat.vektra-ui.test", "https://chat2.other.test"): ("OK", None),
    }
    # with assess enabled the settings refuse a malformed UI origin at load (plan 9); disabled
    # they only warn and keep it, and the item then reports it (review #7)
    with pytest.raises(ValueError):
        settings(VEKTRA_ASSESS_UI_ORIGIN="not-a-url")
    with pytest.warns(UserWarning):
        dormant = settings(
            VEKTRA_ASSESS_ENABLED="false", VEKTRA_ASSESS_UI_ORIGIN="not-a-url"
        )
    _r, items, _o = await good(branch, cfg=dormant)
    assert items["ui_not_same_origin"] == ("UNVERIFIABLE", "origin_invalid")
    assert items["ui_origin_distinct"] == ("MISSING", "ui_origin_invalid")
    # the API origin that makes the second case is the UI's own site
    for (ui, api), want in cases.items():
        env = {"VEKTRA_ASSESS_UI_ORIGIN": ui, "VEKTRA_ASSESS_API_PUBLIC_ORIGIN": api}
        if want == ("MISSING", "same_site_as_api"):
            env["VEKTRA_ASSESS_API_PUBLIC_ORIGIN"] = "https://api.vektra-ui.test"
        _r, items, _o = await good(branch, cfg=settings(**env))
        assert items["ui_not_same_origin"] == want, (ui, api)
    unset = VektraAssessSettings(
        **{k: v for k, v in ENV.items() if "UI_ORIGIN" not in k}
    )  # type: ignore[arg-type]
    _r, items, _o = await good(branch, cfg=unset)
    assert items["ui_not_same_origin"] == ("UNVERIFIABLE", "ui_origin_unset")
    no_api = VektraAssessSettings(
        **{k: v for k, v in ENV.items() if "API_PUBLIC" not in k}
    )  # type: ignore[arg-type]
    _r, items, _o = await good(branch, cfg=no_api)
    assert items["ui_not_same_origin"] == ("UNVERIFIABLE", "api_origin_unset")


def test_grade_write_denylist(branch: str) -> None:
    for name in GRADE_WRITE_FUNCTIONS:
        assert is_grade_write_function(name)
    for name in (
        "mod_assign_save_grade",
        "mod_assign_save_grades",
        "mod_assign_submit_grading_form",
        "core_grades_update_grades",
        "core_grading_save_definitions",
        "mod_quiz_save_overrides",
        "mod_quiz_delete_overrides",
    ):
        assert is_grade_write_function(name), name
    for name in (
        "mod_quiz_get_overrides",
        "mod_assign_get_grades",
        "core_grading_get_definitions",
        "mod_quiz_get_user_best_grade",
        "gradereport_user_get_grade_items",
        "core_webservice_get_site_info",
    ):
        assert not is_grade_write_function(name), name
    # and not one function of the recorded good service matches
    info = json.loads(
        (FIXTURES / branch / "core_webservice_get_site_info__service.json").read_text()
    )
    assert not [
        f["name"]
        for f in info["response"]["functions"]
        if is_grade_write_function(f["name"])
    ]


async def test_a_site_admin_answer_carries_a_secret_that_is_dropped(
    branch: str,
) -> None:
    """Moodle 5.x adds ``sitesecret`` to site_info for an admin: it must neither make the
    answer ``unknown_shape`` (the check must say ``site_admin``) nor survive in the value."""
    secret = "SITESECRET-0123456789"
    replay = Replay(branch)
    replay.mutate = patch(
        **{SITE_INFO: lambda b: {**b, "userissiteadmin": True, "sitesecret": secret}}
    )
    client = make_client(replay)
    info = await read_site_info(client, SiteRef("s", site_for(branch).base_url))
    assert isinstance(info, Readable) and info.value.user_is_site_admin is True
    assert secret not in repr(info)
    _r, items, _o = await good(branch, mutate=replay.mutate)
    assert items["token_works"] == ("OK", None)
    assert items["not_site_admin"] == ("MISSING", "site_admin")


async def test_ui_origin_distinct_item(branch: str) -> None:
    """Plan 9 (ARCH-076): OK for a UI origin distinct from the site's Moodle origin (https
    outside development); MISSING when unset, equal to the Moodle origin, or not https."""
    from vektra_assess.ui.origins import url_origin

    _r, items, _o = await good(
        branch
    )  # https://chat.vektra-ui.test vs the fixture site
    assert items["ui_origin_distinct"] == ("OK", None)
    unset = VektraAssessSettings(
        **{k: v for k, v in ENV.items() if "UI_ORIGIN" not in k}
    )  # type: ignore[arg-type]
    _r, items, _o = await good(branch, cfg=unset)
    assert items["ui_origin_distinct"] == ("MISSING", "ui_origin_unset")
    moodle = url_origin(site_for(branch).base_url)
    _r, items, _o = await good(branch, cfg=settings(VEKTRA_ASSESS_UI_ORIGIN=moodle))
    assert items["ui_origin_distinct"] == ("MISSING", "ui_origin_equals_lms_origin")
    _r, items, _o = await good(
        branch,
        cfg=settings(
            VEKTRA_ENV="production", VEKTRA_ASSESS_UI_ORIGIN="http://ui.example"
        ),
    )
    assert items["ui_origin_distinct"] == ("MISSING", "ui_origin_not_https")


async def test_ui_origin_distinct_uses_the_sites_lms_origin_column(
    branch: str,
) -> None:
    """Review #3: the same Moodle origin as the UI router (``lms_origin``, else the origin of
    ``base_url``), through the same normalisation."""
    from vektra_assess.ui.origins import url_origin

    # the column names the UI origin itself, spelled differently: refused
    _r, items, _o = await good(branch, lms_origin="HTTPS://Chat.Vektra-UI.test:443")
    assert items["ui_origin_distinct"] == ("MISSING", "ui_origin_equals_lms_origin")
    # review 5812478913: separate SITES from both the framing origin (the column) and base_url;
    # either one failing fails the item
    moodle = url_origin(site_for(branch).base_url)
    _r, items, _o = await good(
        branch,
        cfg=settings(VEKTRA_ASSESS_UI_ORIGIN=moodle),
        lms_origin="https://moodle-frame.example",
    )
    assert items["ui_origin_distinct"] == ("MISSING", "same_site_as_lms")
    _r, items, _o = await good(
        branch,
        cfg=settings(VEKTRA_ASSESS_UI_ORIGIN="https://chat.moodle-frame.example"),
        lms_origin="https://moodle-frame.example",
    )
    assert items["ui_origin_distinct"] == ("MISSING", "same_site_as_lms_origin")
    _r, items, _o = await good(branch, lms_origin="https://moodle-frame.example")
    assert items["ui_origin_distinct"] == (
        "OK",
        None,
    )  # chat.vektra-ui.test: another site
    # CodeRabbit 4501070007: a stored origin that cannot be used is reported, not skipped
    _r, items, _o = await good(branch, lms_origin="https://u:p@moodle.example")
    assert items["ui_origin_distinct"] == ("MISSING", "lms_origin_invalid")

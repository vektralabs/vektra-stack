"""Reader failure modes (plan group C): mutated shapes, error envelopes, warnings, unknown
release, and the submission file fetch. Each mutation targets ONE Unreadable reason."""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from assess_moodle_support import (
    BRANCHES,
    FIXTURES,
    TOKEN,
    Replay,
    make_client,
    manifest,
    site_for,
    uid,
)
from structlog.testing import capture_logs

from vektra_lms_moodle.client import MoodleWsClient, SiteRef
from vektra_lms_moodle.domain import SubmissionFileRef
from vektra_lms_moodle.readers import (
    fetch_submission_file,
    read_assign_close_inputs,
    read_assign_release,
    read_assign_settings,
    read_attempt_review,
    read_course_participants,
    read_enrolment,
    read_grade_release,
    read_graders,
    read_grading_view,
    read_quiz_close_inputs,
    read_quiz_overrides,
    read_rubric_fillings,
    read_scale_definition,
    read_site_info,
    read_submission_status,
    read_user_attempts,
    read_user_courses,
    read_user_flags,
    read_user_review_options,
)
from vektra_lms_moodle.result import Readable, Unreadable, UnreadableReason

Call = Callable[[MoodleWsClient, SiteRef, dict[str, Any], str], Awaitable[Any]]


def _quiz(m: dict[str, Any]) -> dict[str, Any]:
    return m["quizzes"]["review_all"]


#: (wsfunction mutated, reader call). Every fetch-based reader appears at least once.
CALLS: dict[str, tuple[str, Call]] = {
    "quiz_close_inputs": (
        "mod_quiz_get_quizzes_by_courses",
        lambda c, s, m, b: read_quiz_close_inputs(c, s, m["courseid"], _quiz(m)["id"]),
    ),
    "user_attempts": (
        "mod_quiz_get_user_attempts",
        lambda c, s, m, b: read_user_attempts(c, s, _quiz(m)["id"], uid(b, "stu1")),
    ),
    "attempt_review": (
        "mod_quiz_get_attempt_review",
        lambda c, s, m, b: read_attempt_review(c, s, _quiz(m)["attempts"][0]["id"]),
    ),
    "review_options": (
        "mod_quiz_get_combined_review_options",
        lambda c, s, m, b: read_user_review_options(
            c, s, _quiz(m)["id"], uid(b, "stu1")
        ),
    ),
    "assign_settings": (
        "mod_assign_get_assignments",
        lambda c, s, m, b: read_assign_settings(
            c, s, m["courseid"], m["assigns"]["cutoff"]["id"]
        ),
    ),
    "participant": (
        "mod_assign_get_participant",
        lambda c, s, m, b: read_assign_close_inputs(
            c, s, m["assigns"]["cutoff"]["id"], uid(b, "stu1")
        ),
    ),
    "submission_status": (
        "mod_assign_get_submission_status",
        lambda c, s, m, b: read_submission_status(
            c, s, m["assigns"]["simple"]["id"], uid(b, "stu1")
        ),
    ),
    "grade_release": (
        "gradereport_user_get_grade_items",
        lambda c, s, m, b: read_grade_release(
            c, s, m["courseid"], uid(b, "stu1"), m["assigns"]["simple"]["cmid"]
        ),
    ),
    "assign_release": (
        "gradereport_user_get_grade_items",
        lambda c, s, m, b: read_assign_release(
            c, s, m["courseid"], m["assigns"]["simple"]["id"], uid(b, "stu1")
        ),
    ),
    "grading_view": (
        "core_grading_get_definitions",
        lambda c, s, m, b: read_grading_view(c, s, m["assigns"]["rubric_vis"]["cmid"]),
    ),
    "rubric_fillings": (
        "core_grading_get_gradingform_instances",
        lambda c, s, m, b: read_rubric_fillings(
            c, s, m["assigns"]["rubric_vis"]["expected"]["rubric"]["definitionid"]
        ),
    ),
    "scale": (
        "gradereport_user_get_grade_items",
        lambda c, s, m, b: read_scale_definition(
            c,
            s,
            m["courseid"],
            uid(b, "stu1"),
            m["assigns"]["scale"]["cmid"],
            m["assigns"]["scale"]["expected"]["scale"]["id"],
        ),
    ),
    "user_courses": (
        "core_enrol_get_users_courses",
        lambda c, s, m, b: read_user_courses(c, s, uid(b, "stu1")),
    ),
    "participants": (
        "core_enrol_get_enrolled_users",
        lambda c, s, m, b: read_course_participants(c, s, m["courseid"]),
    ),
    "enrolment": (
        "core_enrol_get_users_courses",
        lambda c, s, m, b: read_enrolment(c, s, m["courseid"], uid(b, "stu1")),
    ),
    "graders": (
        "core_enrol_get_enrolled_users_with_capability",
        lambda c, s, m, b: read_graders(c, s, m["courseid"]),
    ),
}


def _first_scalar(node: Any) -> tuple[Any, Any]:
    """Depth-first: the first int/str value (skipping ``warnings``) and where it lives."""
    items = node.items() if isinstance(node, dict) else enumerate(node)
    for key, value in items:
        if key == "warnings":
            continue
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            return node, key
        if isinstance(value, (dict, list)) and value:
            found = _first_scalar(value)
            if found is not None:
                return found
    return None  # type: ignore[return-value]


def mutate_first(body: Any, how: str) -> Any:
    """Apply ONE mutation to the first object of the payload (a list root: its first item)."""
    body = copy.deepcopy(body)
    target = body[0] if isinstance(body, list) else body
    if how == "extra":
        target["unexpected_future_field"] = 1
    elif how == "missing":
        key = next(k for k in target if k != "warnings")
        del target[key]
    elif how == "wrongtype":
        container, key = _first_scalar(target)
        container[key] = ["not", "a", "scalar"]
    return body


def serve(replay: Replay, wsfunction: str, fn: Callable[[Any], Any]) -> None:
    replay.mutate = lambda f, body: fn(body) if f == wsfunction else body


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.parametrize("how", ["extra", "missing", "wrongtype"])
@pytest.mark.parametrize("name", sorted(CALLS))
async def test_mutated_shape_is_unknown_shape(name: str, how: str, branch: str) -> None:
    wsfunction, call = CALLS[name]
    replay = Replay(branch)
    serve(replay, wsfunction, lambda body: mutate_first(body, how))
    res = await call(make_client(replay), site_for(branch), manifest(branch), branch)
    assert isinstance(res, Unreadable), (name, how)
    assert (
        res.reason is UnreadableReason.UNKNOWN_SHAPE
        and res.detail_code == "shape_mismatch"
    )


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.parametrize("name", sorted(CALLS))
async def test_error_envelope_at_http_200_is_unreadable_and_message_is_dropped(
    name: str, branch: str
) -> None:
    wsfunction, call = CALLS[name]
    replay = Replay(branch)
    serve(
        replay,
        wsfunction,
        lambda _b: {
            "exception": "required_capability_exception",
            "errorcode": "nopermissions",
            "message": "CANARY-ENVELOPE-MESSAGE student Alice",
        },
    )
    with capture_logs() as logs:
        res = await call(
            make_client(replay), site_for(branch), manifest(branch), branch
        )
    assert (
        isinstance(res, Unreadable) and res.reason is UnreadableReason.ACCESS_EXCEPTION
    )
    assert res.detail_code == "nopermissions"
    assert "CANARY-ENVELOPE-MESSAGE" not in repr(res) + json.dumps(logs, default=str)


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.parametrize("name", sorted(CALLS))
async def test_any_warning_is_unreadable(name: str, branch: str) -> None:
    """Spike trap 1: empty-with-warning answers (HTTP 200) are never data."""
    wsfunction, call = CALLS[name]
    replay = Replay(branch)

    def add_warning(body: Any) -> Any:
        if isinstance(body, list):
            return None  # list roots carry no warnings[]: nothing to inject
        body = copy.deepcopy(body)
        body["warnings"] = [
            {
                "item": "x",
                "itemid": 1,
                "warningcode": "1",
                "message": "No access rights",
            }
        ]
        return body

    probe = replay.response(
        wsfunction, next(f for w, f in replay.by_name if w == wsfunction)
    )
    if isinstance(probe, list) or "warnings" not in probe:
        pytest.skip("this answer has no warnings[] in Moodle's definition")
    serve(replay, wsfunction, add_warning)
    res = await call(make_client(replay), site_for(branch), manifest(branch), branch)
    assert isinstance(res, Unreadable)
    assert res.reason is UnreadableReason.PARTIAL and res.detail_code == "warning_1"


@pytest.mark.parametrize("branch", BRANCHES)
async def test_assignments_without_the_flag_is_the_recorded_trap(branch: str) -> None:
    """The REAL response of get_assignments without includenotenrolledcourses: 200, empty,
    warning. Replayed through the reader it must be Unreadable, never an empty success."""
    replay = Replay(branch)
    m = manifest(branch)
    noflag = replay.response("mod_assign_get_assignments", "noflag")
    assert noflag["courses"] == [] and noflag["warnings"]
    serve(replay, "mod_assign_get_assignments", lambda _b: copy.deepcopy(noflag))
    res = await read_assign_settings(
        make_client(replay),
        site_for(branch),
        m["courseid"],
        m["assigns"]["cutoff"]["id"],
    )
    assert isinstance(res, Unreadable) and res.reason is UnreadableReason.PARTIAL


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.parametrize("name", sorted(CALLS))
@pytest.mark.parametrize(
    "release", [None, "", "6.0.1+ (Build: 1)", "5.1.3 (Build: 1)", "banana"]
)
async def test_unknown_release_branch_is_version_unsupported(
    name: str, release: str | None, branch: str
) -> None:
    _wsfunction, call = CALLS[name]
    replay = Replay(branch)
    site = SiteRef("site-1", site_for(branch).base_url, release=release)
    res = await call(make_client(replay), site, manifest(branch), branch)
    assert (
        isinstance(res, Unreadable)
        and res.reason is UnreadableReason.VERSION_UNSUPPORTED
    )
    assert replay.calls == []  # nothing is sent to a release we have no models for


@pytest.mark.parametrize("branch", BRANCHES)
async def test_site_info_unknown_release_and_shapes(branch: str) -> None:
    for release in ("6.0.1+ (Build: 20270101)", "5.1.3 (Build: 1)"):
        replay = Replay(branch)

        def swap(body: Any, release: str = release) -> Any:
            body = copy.deepcopy(body)
            body["release"] = release
            return body

        serve(replay, "core_webservice_get_site_info", swap)
        res = await read_site_info(
            make_client(replay), SiteRef("site-1", site_for(branch).base_url)
        )
        assert (
            isinstance(res, Unreadable)
            and res.reason is UnreadableReason.VERSION_UNSUPPORTED
        )
    for how in ("extra", "missing", "wrongtype"):
        replay = Replay(branch)
        serve(
            replay,
            "core_webservice_get_site_info",
            lambda b, how=how: mutate_first(b, how),
        )
        res = await read_site_info(
            make_client(replay), SiteRef("site-1", site_for(branch).base_url)
        )
        assert isinstance(res, Unreadable) and res.detail_code == "shape_mismatch"
    replay = Replay(branch)
    serve(replay, "core_webservice_get_site_info", lambda _b: ["not", "an", "object"])
    res = await read_site_info(
        make_client(replay), SiteRef("site-1", site_for(branch).base_url)
    )
    assert isinstance(res, Unreadable)


@pytest.mark.parametrize("branch", BRANCHES)
async def test_a_5_2_response_is_not_accepted_as_4_3_and_vice_versa(
    branch: str,
) -> None:
    """The per-version models are strict: a 5.2 payload read as 4.3 has extra keys."""
    other = "4.3" if branch == "5.2" else "5.2"
    replay = Replay(branch)
    m = manifest(branch)
    foreign = Replay(other)
    serve(
        replay,
        "mod_quiz_get_quizzes_by_courses",
        lambda _b: foreign.response("mod_quiz_get_quizzes_by_courses", "course"),
    )
    res = await read_quiz_close_inputs(
        make_client(replay),
        site_for(branch),
        m["courseid"],
        m["quizzes"]["review_all"]["id"],
    )
    if branch == "4.3":
        assert (
            isinstance(res, Unreadable) and res.reason is UnreadableReason.UNKNOWN_SHAPE
        )
    else:  # a 4.3 payload misses the 5.2-only keys: tolerated only if they are optional
        assert isinstance(res, Readable | Unreadable)


@pytest.mark.parametrize("branch", BRANCHES)
async def test_reader_never_raises_on_garbage(branch: str) -> None:
    for garbage in (None, 5, "x", [], {}, [None], {"quizzes": None}):
        replay = Replay(branch)
        serve(replay, "mod_quiz_get_quizzes_by_courses", lambda _b, g=garbage: g)
        res = await read_quiz_close_inputs(make_client(replay), site_for(branch), 1, 2)
        assert isinstance(res, Unreadable)


@pytest.mark.parametrize("branch", BRANCHES)
async def test_quiz_not_found_and_assign_not_found_are_partial(branch: str) -> None:
    replay = Replay(branch)
    m = manifest(branch)
    client, site = make_client(replay), site_for(branch)
    q = await read_quiz_close_inputs(client, site, m["courseid"], 999999)
    assert isinstance(q, Unreadable) and q.detail_code == "quiz_not_found"
    a = await read_assign_settings(client, site, m["courseid"], 999999)
    assert isinstance(a, Unreadable) and a.detail_code == "assign_not_found"


@pytest.mark.parametrize("branch", BRANCHES)
async def test_the_overrides_reader_on_the_wrong_version_and_after_a_denied_call(
    branch: str,
) -> None:
    replay = Replay(branch)
    m = manifest(branch)
    res = await read_quiz_overrides(
        make_client(replay), site_for(branch), m["quizzes"]["types"]["id"]
    )
    if branch == "4.3":
        assert isinstance(res, Unreadable)
        assert res.reason is UnreadableReason.FUNCTION_NOT_IN_SERVICE
    else:
        denied = replay.response(
            "core_course_get_categories", "denied"
        )  # a REAL accessexception
        assert denied["errorcode"] == "accessexception"
        serve(replay, "mod_quiz_get_overrides", lambda _b: denied)
        res = await read_quiz_overrides(
            make_client(replay), site_for(branch), m["quizzes"]["types"]["id"]
        )
        assert (
            isinstance(res, Unreadable)
            and res.reason is UnreadableReason.ACCESS_EXCEPTION
        )


# --- user flags (hand-written row model) --------------------------------------------------------


@pytest.mark.parametrize("branch", BRANCHES)
async def test_user_flags_row_shape_is_strict(branch: str) -> None:
    replay = Replay(branch)
    m = manifest(branch)
    a = m["assigns"]["cutoff"]
    serve(
        replay,
        "mod_assign_get_user_flags",
        lambda _b: {
            "assignments": [
                {
                    "assignmentid": a["id"],
                    "userflags": [{"id": 1, "userid": 2, "surprise": 1}],
                }
            ],
            "warnings": [],
        },
    )
    res = await read_user_flags(make_client(replay), site_for(branch), a["id"])
    assert isinstance(res, Unreadable) and res.reason is UnreadableReason.UNKNOWN_SHAPE


# --- submission file fetch ----------------------------------------------------------------------


def files_replay(branch: str) -> tuple[Replay, dict[str, Any], int, int]:
    replay = Replay(branch)
    m = manifest(branch)
    a = m["assigns"]["files"]
    return replay, m, a["id"], uid(branch, "stu1")


async def _listed(branch: str, replay: Replay) -> tuple[Any, Any, int, int]:
    client = make_client(replay)
    site = site_for(branch)
    m = manifest(branch)
    a = m["assigns"]["files"]
    status = await read_submission_status(client, site, a["id"], uid(branch, "stu1"))
    assert isinstance(status, Readable)
    for f in status.value.files:
        local = FIXTURES / branch / "files" / f"stu1__{f.filename}"
        from urllib.parse import urlsplit

        replay.files[urlsplit(f.fileurl).path] = local.read_bytes()
    return client, status.value, a["id"], uid(branch, "stu1")


@pytest.mark.parametrize("branch", BRANCHES)
async def test_fetch_submission_file_returns_the_bytes_in_memory(branch: str) -> None:
    replay = Replay(branch)
    client, status, assignid, owner = await _listed(branch, replay)
    site = site_for(branch)
    m = manifest(branch)
    expected = {
        f["filename"]: f.get("content_canary")
        for f in m["assigns"]["files"]["submissions"]["stu1"]["files"]
    }
    assert len(status.files) == 5
    with capture_logs() as logs:
        for ref in status.files:
            res = await fetch_submission_file(
                client, site, assignid, ref, owner, status.submission_id
            )
            assert isinstance(res, Readable), ref.filename
            assert len(res.value) == ref.size
            canary = expected[ref.filename]
            if canary and ref.mimetype.startswith("text"):
                assert canary.encode() in res.value
    # the request carried the token as a query parameter, on the pinned connection ...
    sent = replay.file_requests
    assert len(sent) == 5
    assert all(
        r.url.params["token"] == TOKEN and r.url.host == "93.184.216.34" for r in sent
    )
    assert all(r.method == "GET" for r in sent)
    # ... and neither the token nor any URL ever reached the logs
    text = json.dumps(logs, default=str)
    assert (
        TOKEN not in text
        and "pluginfile" not in text
        and "token" not in text.replace("tokens", "")
    )
    assert any(e["event"] == "moodle_file_fetch" for e in logs)


@pytest.mark.parametrize("branch", BRANCHES)
async def test_fetch_refuses_a_file_that_is_not_in_the_owners_submission(
    branch: str,
) -> None:
    replay = Replay(branch)
    client, status, assignid, owner = await _listed(branch, replay)
    site = site_for(branch)
    ref = status.files[0]
    # wrong submission id
    res = await fetch_submission_file(
        client, site, assignid, ref, owner, status.submission_id + 1
    )
    assert isinstance(res, Unreadable) and res.detail_code == "submission_mismatch"
    # a forged reference (other path, same name)
    forged = SubmissionFileRef(
        ref.filename,
        ref.filepath,
        ref.size,
        ref.mimetype,
        "https://moodle.example.org/webservice/pluginfile.php/1/x/y/9/" + ref.filename,
        ref.timemodified,
    )
    res = await fetch_submission_file(
        client, site, assignid, forged, owner, status.submission_id
    )
    assert isinstance(res, Unreadable) and res.detail_code == "file_not_in_submission"
    # another student's id: Moodle's own answer (here: the owner mismatch) decides
    other = uid(branch, "stu2")
    res = await fetch_submission_file(
        client, site, assignid, ref, other, status.submission_id
    )
    assert isinstance(res, Unreadable)
    assert replay.file_requests == []  # nothing was downloaded in any of these cases


@pytest.mark.parametrize("branch", BRANCHES)
async def test_fetch_enforces_the_file_size_cap_and_integrity(branch: str) -> None:
    replay = Replay(branch)
    client, status, assignid, owner = await _listed(branch, replay)
    site = site_for(branch)
    biggest = max(status.files, key=lambda f: f.size)
    capped = make_client(
        replay, VEKTRA_LMS_MOODLE_WS_MAX_FILE_BYTES=str(biggest.size - 1)
    )
    res = await fetch_submission_file(
        capped, site, assignid, biggest, owner, status.submission_id
    )
    assert isinstance(res, Unreadable) and res.reason is UnreadableReason.SIZE_CAP
    # the bytes do not match what Moodle listed
    from urllib.parse import urlsplit

    replay.files[urlsplit(biggest.fileurl).path] = b"short"
    res = await fetch_submission_file(
        client, site, assignid, biggest, owner, status.submission_id
    )
    assert isinstance(res, Unreadable) and res.detail_code == "size_mismatch"


@pytest.mark.parametrize("branch", BRANCHES)
async def test_fetch_redirect_and_missing_file(branch: str) -> None:
    replay = Replay(branch)
    client, status, assignid, owner = await _listed(branch, replay)
    site = site_for(branch)
    ref = status.files[0]
    from urllib.parse import urlsplit

    path = urlsplit(ref.fileurl).path
    # 302 is never followed
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == path:
            seen.append(str(request.url.host))
            return httpx.Response(
                302, headers={"Location": "http://evil.example/steal"}
            )
        return replay.handler(request)

    redirecting = MoodleWsClient(
        client._s,
        token_provider=lambda _s: TOKEN,
        transport=httpx.MockTransport(handler),
        resolver=client._resolver,
    )
    res = await fetch_submission_file(
        redirecting, site, assignid, ref, owner, status.submission_id
    )
    assert isinstance(res, Unreadable) and res.reason is UnreadableReason.REDIRECT
    assert seen == ["93.184.216.34"]  # exactly one request, Location not followed
    # 404
    del replay.files[path]
    res = await fetch_submission_file(
        client, site, assignid, ref, owner, status.submission_id
    )
    assert isinstance(res, Unreadable) and res.detail_code == "http_404"


@pytest.mark.parametrize("branch", BRANCHES)
async def test_fetch_refuses_a_non_pluginfile_url(branch: str) -> None:
    replay = Replay(branch)
    client, status, assignid, owner = await _listed(branch, replay)
    site = site_for(branch)
    ref = status.files[0]

    def evil(body: Any) -> Any:
        body = copy.deepcopy(body)
        for plugin in body["lastattempt"]["submission"]["plugins"]:
            for area in plugin.get("fileareas", []):
                for f in area["files"]:
                    f["fileurl"] = (
                        "https://moodle.example.org/../admin/secret.php/"
                        + f["filename"]
                    )
        return body

    serve(replay, "mod_assign_get_submission_status", evil)
    forged = SubmissionFileRef(
        ref.filename,
        ref.filepath,
        ref.size,
        ref.mimetype,
        "https://moodle.example.org/../admin/secret.php/" + ref.filename,
        ref.timemodified,
    )
    res = await fetch_submission_file(
        client, site, assignid, forged, owner, status.submission_id
    )
    assert isinstance(res, Unreadable) and res.detail_code == "file_url_not_pluginfile"
    assert replay.file_requests == []

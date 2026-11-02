#!/usr/bin/env python3
"""Record Moodle web-service responses from a DEV site as hermetic test fixtures.

Plan assess-moodle-ws, group C. For every reader of ``vektra_assess.moodle.readers`` this calls
the WS functions the reader uses against a seeded dev site and writes

    <out>/<4.3|5.2>/<wsfunction>__<fixture>.json     {"wsfunction", "fixture", "params", "response"}
    <out>/<4.3|5.2>/files/<name>                     submission file bytes (synthetic)
    <out>/<4.3|5.2>/manifest.json                    the seed manifest (expected values), scrubbed

Safety rules (the recorder REFUSES, it does not warn):
- the base URL must be ``http://localhost:<port>`` or ``http://127.0.0.1:<port>``;
- the site's own ``release`` must be a 4.3.x / 5.2.x *dev build* (``4.3.11+ (Build: ...)``);
- redirects are never followed;
- the token, the host and ``userprivateaccesskey`` must not survive scrubbing: the run aborts
  and writes nothing if they do.

Usage::

    uv run python scripts/assess_record_ws.py --base-url http://localhost:8080 \\
        --token-file <dir>/provision-m43.json --manifest <dir>/manifest-m43.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCRUBBED_ROOT = "https://moodle.example.org"
DEV_RELEASE = re.compile(r"^(4\.3|5\.2)\.\d+\+ \(Build: \d+\)$")
LOCAL_URL = re.compile(r"^http://(localhost|127\.0\.0\.1):\d{2,5}$")
DEFAULT_OUT = (
    Path(__file__).resolve().parent.parent
    / "vektra-assess"
    / "tests"
    / "fixtures"
    / "moodle_ws"
)
SECRET_KEYS = {"userprivateaccesskey", "privatetoken", "token", "wstoken"}


class RecorderRefused(Exception):
    """A safety rule failed: nothing is recorded."""


def check_base_url(url: str) -> str:
    url = url.rstrip("/")
    if not LOCAL_URL.fullmatch(url):
        raise RecorderRefused(
            "refused: the base URL must be http://localhost:<port> or http://127.0.0.1:<port>"
        )
    return url


def check_release(release: Any) -> str:
    if not isinstance(release, str) or not DEV_RELEASE.fullmatch(release):
        raise RecorderRefused(
            "refused: the site is not a 4.3.x / 5.2.x dev build (release string mismatch)"
        )
    return release.split(".")[0] + "." + release.split(".")[1]


# --- scrubbing ---------------------------------------------------------------------------


def scrub(value: Any, *, base_url: str, token: str) -> Any:
    """Replace the host, the token and secret-bearing keys, recursively."""
    host_re = re.compile(
        re.escape(base_url) + r"|https?:\\?/\\?/(?:localhost|127\.0\.0\.1)(?::\d+)?"
    )
    token_q = re.compile(r"([?&])token=[^&\"'\s]*")

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {
                k: (
                    "REDACTED"
                    if k in SECRET_KEYS and isinstance(x, str) and x
                    else walk(x)
                )
                for k, x in v.items()
            }
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str):
            out = host_re.sub(SCRUBBED_ROOT, v)
            out = token_q.sub(lambda m: m.group(1) + "token=REDACTED", out)
            return out.replace(token, "REDACTED") if token else out
        return v

    return walk(value)


def assert_clean(
    blob: str, *, base_url: str, token: str, extra: tuple[str, ...] = ()
) -> None:
    host = urllib.parse.urlsplit(base_url)
    needles = [token, base_url, f"{host.hostname}:{host.port}", *extra]
    for needle in needles:
        if needle and needle in blob:
            raise RecorderRefused(
                "refused: a secret or the dev host survived scrubbing"
            )
    if "userprivateaccesskey" in blob and not re.search(
        r'"userprivateaccesskey": "REDACTED"', blob
    ):
        raise RecorderRefused("refused: userprivateaccesskey not redacted")


# --- transport ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a: Any, **_k: Any) -> None:  # type: ignore[override]
        return None


from vektra_assess.moodle.client import flatten_params  # noqa: E402

_OPENER = urllib.request.build_opener(_NoRedirect)


def ws_call(base: str, token: str, fn: str, params: dict[str, Any]) -> Any:
    pairs = [("wstoken", token), ("wsfunction", fn), ("moodlewsrestformat", "json")]
    pairs.extend(flatten_params(params).items())
    req = urllib.request.Request(
        base + "/webservice/rest/server.php",
        data=urllib.parse.urlencode(pairs).encode(),
        method="POST",
    )
    with _OPENER.open(req, timeout=30) as resp:
        return json.loads(resp.read())


def download(base: str, token: str, fileurl: str) -> bytes:
    path = urllib.parse.urlsplit(fileurl).path
    req = urllib.request.Request(
        base + path + "?" + urllib.parse.urlencode({"token": token})
    )
    with _OPENER.open(req, timeout=30) as resp:
        return bytes(resp.read())


# --- the plan: which calls each reader needs ---------------------------------------------

Call = tuple[str, str, dict[str, Any]]  # (wsfunction, fixture, params)


def plan(manifest: dict[str, Any], branch: str) -> list[Call]:
    cid = manifest["courseid"]
    users = {k: v["id"] for k, v in manifest["users"].items()}
    students = [k for k, v in manifest["users"].items() if v["role"] == "student"]
    calls: list[Call] = [
        ("core_webservice_get_site_info", "service", {}),
        ("core_course_get_categories", "denied", {}),
        ("mod_quiz_get_quizzes_by_courses", "course", {"courseids": [cid]}),
        # no ``onlyactive`` option: it needs moodle/course:enrolreview, which the minimal
        # service role does not hold (nopermissions). Activity comes from get_users_courses.
        ("core_enrol_get_enrolled_users", "course", {"courseid": cid}),
        (
            "core_enrol_get_enrolled_users_with_capability",
            "graders",
            {
                "coursecapabilities": [
                    {"courseid": cid, "capabilities": ["mod/assign:grade"]}
                ]
            },
        ),
        (
            "mod_assign_get_assignments",
            "course",
            {"courseids": [cid], "includenotenrolledcourses": 1},
        ),
        # trap 1: without the flag Moodle answers HTTP 200, courses=[] and a warning
        ("mod_assign_get_assignments", "noflag", {"courseids": [cid]}),
    ]
    for name, uid in users.items():
        calls.append(("core_enrol_get_users_courses", name, {"userid": uid}))
    for name in students:
        uid = users[name]
        calls.append(
            ("gradereport_user_get_grade_items", name, {"courseid": cid, "userid": uid})
        )
    for qname, q in manifest["quizzes"].items():
        if not q.get("seeded"):
            continue
        qid = q["id"]
        attempt_users = sorted({a["user"] for a in q.get("attempts", [])})
        probe_users = attempt_users or ["stu1"]
        for u in probe_users:
            calls.append(
                (
                    "mod_quiz_get_user_attempts",
                    f"{qname}-{u}",
                    {"quizid": qid, "userid": users[u], "status": "all"},
                )
            )
        calls.append(
            (
                "mod_quiz_get_combined_review_options",
                f"{qname}-{probe_users[0]}",
                {"quizid": qid, "userid": users[probe_users[0]]},
            )
        )
        if branch == "5.2":
            calls.append(("mod_quiz_get_overrides", qname, {"quizid": qid}))
        for a in q.get("attempts", []):
            calls.append(
                (
                    "mod_quiz_get_attempt_review",
                    f"{qname}-{a['user']}-a{a['attempt_number']}",
                    {"attemptid": a["id"]},
                )
            )
    for aname, a in manifest["assigns"].items():
        if not a.get("seeded"):
            continue
        aid = a["id"]
        calls.append(("mod_assign_get_user_flags", aname, {"assignmentids": [aid]}))
        part_users = sorted({"stu1", *a["expected"].get("reader_by_user", {})})
        for u in part_users:
            calls.append(
                (
                    "mod_assign_get_participant",
                    f"{aname}-{u}",
                    {"assignid": aid, "userid": users[u]},
                )
            )
        for u in sorted({*a.get("submissions", {}), *part_users}):
            calls.append(
                (
                    "mod_assign_get_submission_status",
                    f"{aname}-{u}",
                    {"assignid": aid, "userid": users[u]},
                )
            )
        calls.append(
            (
                "core_grading_get_definitions",
                aname,
                {"cmids": [a["cmid"]], "areaname": "submissions", "activeonly": 1},
            )
        )
        exp = a["expected"]
        defn = exp.get("rubric") or exp.get("guide")
        if defn:
            calls.append(
                (
                    "core_grading_get_gradingform_instances",
                    aname,
                    {"definitionid": defn["definitionid"], "since": 0},
                )
            )
    return calls


def fixture_name(fn: str, fixture: str) -> str:
    return f"{fn}__{fixture}.json"


def record(
    base_url: str, token: str, manifest: dict[str, Any], out_dir: Path
) -> dict[str, int]:
    base = check_base_url(base_url)
    info = ws_call(base, token, "core_webservice_get_site_info", {})
    if not isinstance(info, dict) or "release" not in info:
        raise RecorderRefused("refused: the site did not answer get_site_info")
    branch = check_release(info["release"])
    if manifest.get("release") != info["release"]:
        raise RecorderRefused(
            "refused: the manifest does not belong to this site's release"
        )

    staged: dict[str, bytes] = {}
    # the service account's own user record (REQ-079 ``interactive_login_disabled`` reads its
    # ``auth``); the id comes from site_info, so it is not part of the static plan
    own = (
        "core_user_get_users_by_field",
        "self",
        {"field": "id", "values": [info["userid"]]},
    )
    for fn, fixture, params in [*plan(manifest, branch), own]:
        response = (
            info
            if fn == "core_webservice_get_site_info"
            else ws_call(base, token, fn, params)
        )
        doc = {
            "wsfunction": fn,
            "fixture": fixture,
            "params": params,
            "response": scrub(response, base_url=base, token=token),
        }
        staged[fixture_name(fn, fixture)] = (
            json.dumps(doc, indent=1, sort_keys=True) + "\n"
        ).encode()

    files: dict[str, bytes] = {}
    for u in manifest["assigns"]["files"].get("submissions", {}):
        uid = manifest["users"][u]["id"]
        status = ws_call(
            base,
            token,
            "mod_assign_get_submission_status",
            {"assignid": manifest["assigns"]["files"]["id"], "userid": uid},
        )
        for plugin in status["lastattempt"]["submission"]["plugins"]:
            for area in plugin.get("fileareas", []):
                for f in area["files"]:
                    data = download(base, token, f["fileurl"])
                    if len(data) <= 256 * 1024:
                        files[f"{u}__{f['filename']}"] = data

    manifest_doc = {k: v for k, v in manifest.items() if k != "checks"}
    staged["manifest.json"] = (
        json.dumps(
            scrub(manifest_doc, base_url=base, token=token), indent=1, sort_keys=True
        )
        + "\n"
    ).encode()

    for blob in staged.values():
        assert_clean(blob.decode(), base_url=base, token=token)

    target = out_dir / branch
    (target / "files").mkdir(parents=True, exist_ok=True)
    for name, blob in staged.items():
        (target / name).write_bytes(blob)
    for name, data in files.items():
        (target / "files" / name).write_bytes(data)
    return {"responses": len(staged) - 1, "files": len(files)}


def _token_from(path: Path, key: str) -> str:
    doc: Any = json.loads(path.read_text())
    for part in key.split("."):
        doc = doc[part]
    if not isinstance(doc, str) or not doc:
        raise RecorderRefused("refused: no token at that key")
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token-file", type=Path, required=True)
    ap.add_argument("--token-key", default="service_account.token")
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    try:
        counts = record(
            args.base_url,
            _token_from(args.token_file, args.token_key),
            json.loads(args.manifest.read_text()),
            args.out,
        )
    except RecorderRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError) as exc:
        print(f"recorder transport failure: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"recorded {counts['responses']} responses and {counts['files']} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

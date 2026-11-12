"""REQ-079 setup check, the Moodle provider's part: the service-account items (ADR-0033 D3).

The runner, the report lifecycle and Vektra's own items are generic (``vektra_assess.setup``);
``MoodleLmsProvider.service_account_check`` returns these items, in ``SERVICE_ACCOUNT_ITEMS``
order:

- ``token_works``: ``core_webservice_get_site_info`` answers;
- ``required_reads_quiz|assign|grade|grading|enrol|files``: one per reader group, every reader
  of the group is Readable on the probe course (value checks are the probes' job);
- ``interactive_login_disabled``: ``core_user_get_users_by_field(id, self)`` says
  ``auth == 'webservice'`` (``nologin`` cannot be used: WS refuses it);
- ``not_site_admin``: ``site_info.userissiteadmin`` is false;
- ``not_ingestion_account``: site_info username differs from
  ``VEKTRA_LMS_MOODLE_INGEST_ACCOUNT_USERNAME`` (unset: UNVERIFIABLE);
- ``denied_function_probe``: a harmless read-only function left out of the service
  (``core_course_get_categories``) answers ``accessexception``; a write function is never called;
- ``no_grade_write``: no grade-write function in ``site_info.functions`` (hard-coded denylist);
- ``ws_tls_ok``: the call went through verified TLS (plain http only in development).

Every item is ``OK``, ``MISSING`` or ``UNVERIFIABLE``, content-free, and an item that raises is
``UNVERIFIABLE/check_error`` (fail closed).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import structlog

from vektra_lms_moodle.client import MoodleWsClient, SiteRef
from vektra_lms_moodle.config import MoodleSettings
from vektra_lms_moodle.domain import SiteInfo
from vektra_lms_moodle.readers import (
    fetch_submission_file,
    read_assign_close_inputs,
    read_assign_settings,
    read_attempt_review,
    read_enrolment,
    read_grade_release,
    read_graders,
    read_grading_view,
    read_own_auth,
    read_quiz_close_inputs,
    read_rubric_fillings,
    read_submission_status,
    read_user_attempts,
    read_user_review_options,
)
from vektra_lms_moodle.result import Readable, Reading, Unreadable, UnreadableReason
from vektra_shared.lms import (
    SETUP_MISSING,
    SETUP_OK,
    SETUP_UNVERIFIABLE,
    SetupItem,
)

log = structlog.get_logger(__name__)


DENIED_FUNCTION = "core_course_get_categories"

_CODE = re.compile(r"[a-z0-9_]{1,64}")


# An item's outcome is the Protocol's ``SetupItem`` itself, its name left empty here and set
# by ``run_service_account_items``: the vocabulary is the one the runner and the report use.


def ok(reason: str | None = None) -> SetupItem:
    return SetupItem("", SETUP_OK, reason)


def missing(reason: str) -> SetupItem:
    return SetupItem("", SETUP_MISSING, reason)


def unverifiable(reason: str) -> SetupItem:
    return SetupItem("", SETUP_UNVERIFIABLE, reason)


@dataclass(frozen=True)
class CheckContext:
    client: MoodleWsClient
    site: SiteRef  # carries the release when site_info answered
    settings: MoodleSettings
    manifest: Mapping[str, Any] | None  # the probe manifest of the site
    info: Reading[SiteInfo]  # site_info, read once for the whole run


ItemCheck = Callable[[CheckContext], Awaitable[SetupItem]]


def _code(value: str, default: str = "other") -> str:
    v = value.lower()
    return v if _CODE.fullmatch(v) else default


def _unreadable_outcome(u: Unreadable) -> SetupItem:
    return unverifiable(u.reason.value)


# --- items ----------------------------------------------------------------------------------------


def _info(ctx: CheckContext) -> SiteInfo | SetupItem:
    """The site_info, or the outcome every WS-dependent item reports without calling."""
    if isinstance(ctx.info, Readable):
        return ctx.info.value
    if ctx.info.reason is UnreadableReason.ACCESS_EXCEPTION:
        return missing("token_works_failed")
    return unverifiable("site_unreachable")


async def token_works(ctx: CheckContext) -> SetupItem:
    if isinstance(ctx.info, Readable):
        return ok()
    code = _code(ctx.info.detail_code, ctx.info.reason.value)
    if ctx.info.reason is UnreadableReason.ACCESS_EXCEPTION:
        return missing(code)
    return unverifiable(ctx.info.reason.value)


async def interactive_login_disabled(ctx: CheckContext) -> SetupItem:
    info = _info(ctx)
    if isinstance(info, SetupItem):
        return info
    auth = await read_own_auth(ctx.client, ctx.site, info.userid)
    if isinstance(auth, Unreadable):
        return _unreadable_outcome(auth)
    if auth.value == "webservice":
        return ok()
    return missing(_code(f"auth_{auth.value}", "auth_other"))


async def not_site_admin(ctx: CheckContext) -> SetupItem:
    info = _info(ctx)
    if isinstance(info, SetupItem):
        return info
    return missing("site_admin") if info.user_is_site_admin else ok()


async def not_ingestion_account(ctx: CheckContext) -> SetupItem:
    info = _info(ctx)
    if isinstance(info, SetupItem):
        return info
    ingest = ctx.settings.ingest_account_username
    if not ingest:
        return unverifiable("ingest_username_unset")
    if info.username.casefold() == ingest.casefold():
        return missing("is_ingestion_account")
    return ok()


async def denied_function_probe(ctx: CheckContext) -> SetupItem:
    """Calls a read-only function that is NOT in the service: it must be refused."""
    info = _info(ctx)
    if isinstance(info, SetupItem):
        return info
    res = await ctx.client.call(ctx.site, DENIED_FUNCTION, {})
    if isinstance(res, Readable):
        return missing("denied_function_succeeded")
    if (
        res.reason is UnreadableReason.ACCESS_EXCEPTION
        and res.detail_code == "accessexception"
    ):
        return ok()
    return unverifiable(res.reason.value)


#: functions that write grades, overrides or grading definitions. The control of REQ-079 is the
#: function list of the service, not the capability set (amended 2026-11-01).
GRADE_WRITE_FUNCTIONS: frozenset[str] = frozenset(
    {
        "mod_assign_save_grade",
        "mod_assign_save_grades",
        "mod_assign_submit_grading_form",
        "mod_assign_save_user_extensions",
        "core_grades_update_grades",
        "core_grades_create_gradecategories",
        "core_grading_save_definitions",
        "mod_quiz_save_overrides",
        "mod_quiz_delete_overrides",
        "mod_quiz_regrade_attempts",
    }
)
_GRADE_WRITE_PATTERN = re.compile(
    r"^(?:core_grades_(?:update|create|save|delete)_.*"
    r"|core_grading_(?:save|update|delete)_.*"
    r"|mod_assign_(?:save|submit)_grad.*"
    r"|mod_quiz_(?:save|delete|set|update|add|regrade)_.*(?:override|regrade|grade).*)$"
)


def is_grade_write_function(name: str) -> bool:
    return name in GRADE_WRITE_FUNCTIONS or bool(_GRADE_WRITE_PATTERN.match(name))


async def no_grade_write(ctx: CheckContext) -> SetupItem:
    info = _info(ctx)
    if isinstance(info, SetupItem):
        return info
    if any(is_grade_write_function(f) for f in info.functions):
        return missing("grade_write_function_in_service")
    return ok()


async def ws_tls_ok(ctx: CheckContext) -> SetupItem:
    """The WS call went through verified TLS. Plain http can only have worked in
    ``VEKTRA_ENV=development`` with ``..._ALLOW_HTTP=true`` (the client refuses it otherwise):
    it is reported as OK with its own reason code so a dev run is never mistaken for TLS."""
    if isinstance(ctx.info, Unreadable):
        if ctx.info.reason is UnreadableReason.TLS:
            return missing(_code(ctx.info.detail_code, "tls"))
        return unverifiable("site_unreachable")
    scheme = urlsplit(ctx.site.base_url).scheme
    return ok() if scheme == "https" else ok("development_plain_http")


# --- required reads (one item per reader group) -----------------------------------------------------


def _first_failure(readings: Sequence[Reading[Any]]) -> SetupItem | None:
    for r in readings:
        if isinstance(r, Unreadable):
            return missing(f"unreadable_{r.reason.value}")
    return None


def _reads(ctx: CheckContext, group: str) -> Mapping[str, Any] | SetupItem:
    info = _info(ctx)
    if isinstance(info, SetupItem):
        return info
    args = (ctx.manifest or {}).get("required_reads", {}).get(group)
    if not args:
        return unverifiable("no_probe_manifest")
    return {**args, "courseid": int((ctx.manifest or {}).get("courseid", 0))}


async def required_reads_quiz(ctx: CheckContext) -> SetupItem:
    a = _reads(ctx, "quiz")
    if isinstance(a, SetupItem):
        return a
    c, s = ctx.client, ctx.site
    readings: list[Reading[Any]] = [
        await read_quiz_close_inputs(c, s, a["courseid"], a["quizid"]),
        await read_user_attempts(c, s, a["quizid"], a["userid"]),
        await read_attempt_review(c, s, a["attemptid"]),
        await read_user_review_options(c, s, a["quizid"], a["userid"]),
    ]
    return _first_failure(readings) or ok()


async def required_reads_assign(ctx: CheckContext) -> SetupItem:
    a = _reads(ctx, "assign")
    if isinstance(a, SetupItem):
        return a
    c, s = ctx.client, ctx.site
    readings: list[Reading[Any]] = [
        await read_assign_settings(c, s, a["courseid"], a["assignid"]),
        await read_assign_close_inputs(c, s, a["assignid"], a["userid"]),
        await read_submission_status(c, s, a["assignid"], a["userid"]),
    ]
    return _first_failure(readings) or ok()


async def required_reads_grade(ctx: CheckContext) -> SetupItem:
    a = _reads(ctx, "grade")
    if isinstance(a, SetupItem):
        return a
    res = await read_grade_release(
        ctx.client, ctx.site, a["courseid"], a["userid"], a["cmid"]
    )
    return _first_failure([res]) or ok()


async def required_reads_grading(ctx: CheckContext) -> SetupItem:
    a = _reads(ctx, "grading")
    if isinstance(a, SetupItem):
        return a
    readings: list[Reading[Any]] = [
        await read_grading_view(ctx.client, ctx.site, a["cmid"]),
        await read_rubric_fillings(ctx.client, ctx.site, a["definitionid"]),
    ]
    return _first_failure(readings) or ok()


async def required_reads_enrol(ctx: CheckContext) -> SetupItem:
    a = _reads(ctx, "enrol")
    if isinstance(a, SetupItem):
        return a
    readings: list[Reading[Any]] = [
        await read_enrolment(ctx.client, ctx.site, a["courseid"], a["userid"]),
        await read_graders(ctx.client, ctx.site, a["courseid"]),
    ]
    return _first_failure(readings) or ok()


async def required_reads_files(ctx: CheckContext) -> SetupItem:
    """The service may download files and the smallest file of the probe submission arrives
    with the size Moodle announced."""
    a = _reads(ctx, "files")
    if isinstance(a, SetupItem):
        return a
    info = _info(ctx)
    assert not isinstance(info, SetupItem)
    if not info.downloadfiles:
        return missing("downloadfiles_disabled")
    status = await read_submission_status(
        ctx.client, ctx.site, a["assignid"], a["userid"]
    )
    if isinstance(status, Unreadable):
        return missing(f"unreadable_{status.reason.value}")
    if not status.value.files or status.value.submission_id is None:
        return unverifiable("no_probe_file")
    ref = min(status.value.files, key=lambda f: f.size)
    got = await fetch_submission_file(
        ctx.client,
        ctx.site,
        a["assignid"],
        ref,
        a["userid"],
        status.value.submission_id,
    )
    return _first_failure([got]) or ok()


# --- the provider's part of a run -----------------------------------------------------------------

#: the service-account items, in report order (``MoodleLmsProvider.setup_items``)
ITEMS: tuple[tuple[str, ItemCheck], ...] = (
    ("token_works", token_works),
    ("required_reads_quiz", required_reads_quiz),
    ("required_reads_assign", required_reads_assign),
    ("required_reads_grade", required_reads_grade),
    ("required_reads_grading", required_reads_grading),
    ("required_reads_enrol", required_reads_enrol),
    ("required_reads_files", required_reads_files),
    ("interactive_login_disabled", interactive_login_disabled),
    ("not_site_admin", not_site_admin),
    ("not_ingestion_account", not_ingestion_account),
    ("denied_function_probe", denied_function_probe),
    ("no_grade_write", no_grade_write),
    ("ws_tls_ok", ws_tls_ok),
)
SERVICE_ACCOUNT_ITEMS: tuple[str, ...] = tuple(name for name, _ in ITEMS)


async def run_service_account_items(ctx: CheckContext) -> tuple[SetupItem, ...]:
    """Every service-account item, in ``SERVICE_ACCOUNT_ITEMS`` order; an item that raises or
    answers outside the vocabulary is ``UNVERIFIABLE/check_error``."""
    out: list[SetupItem] = []
    for name, check in ITEMS:
        try:
            outcome = await check(ctx)
        except Exception as exc:
            log.warning(
                "setup_check_item_error", item=name, error_type=type(exc).__name__
            )
            outcome = unverifiable("check_error")
        if outcome.result not in (SETUP_OK, SETUP_MISSING, SETUP_UNVERIFIABLE):
            outcome = unverifiable("check_error")
        out.append(dataclasses.replace(outcome, name=name))
    return tuple(out)

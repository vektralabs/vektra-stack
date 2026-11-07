"""REQ-079 setup check: the items, the run, and what a revoked token does.

Every item is ``OK``, ``MISSING`` or ``UNVERIFIABLE``; only ``OK`` counts (UNVERIFIABLE is not
OK) and every unknown fails closed. A report is content-free: ``{name, result, reason_code}``
per item, nothing Moodle said, no id.

Built-in items (registered here):

- ``token_works``: ``core_webservice_get_site_info`` answers;
- ``required_reads_quiz|assign|grade|grading|enrol|files``: one per reader group, every reader
  of the group is Readable on the probe course (value checks are the probes' job);
- ``interactive_login_disabled``: ``core_user_get_users_by_field(id, self)`` says
  ``auth == 'webservice'`` (``nologin`` cannot be used: WS refuses it);
- ``not_site_admin``: ``site_info.userissiteadmin`` is false;
- ``not_ingestion_account``: site_info username differs from
  ``VEKTRA_ASSESS_INGEST_ACCOUNT_USERNAME`` (unset: UNVERIFIABLE);
- ``denied_function_probe``: a harmless read-only function left out of the service
  (``core_course_get_categories``) answers ``accessexception``; a write function is never called;
- ``no_grade_write``: no grade-write function in ``site_info.functions`` (hard-coded denylist);
- ``ws_tls_ok``: the call went through verified TLS (plain http only in development);
- ``ui_not_same_origin``: the chat UI origin is not the same site as Moodle or the API.

Pluggable items (``parser_sidecar_running``, ``smtp_probe``, ``db_log_statement_none``) are
registered by the plans that own them; an unregistered one is ``MISSING`` (fail closed).

Revoked token: ``Unreadable(access_exception)`` on ``token_works`` makes the site NotReady (a
done report with ``token_works`` MISSING for the current release), writes the audit event
``assess_ws_token_rejected`` in the SAME transaction as the report, and emits one operator alert
log event (metadata only, no token).
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from vektra_assess.capabilities import observe_release
from vektra_assess.config import VektraAssessSettings
from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.domain import SiteInfo
from vektra_assess.moodle.probes import FlagResult, persist_matrix, run_probes
from vektra_assess.moodle.readers import (
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
    read_site_info,
    read_submission_status,
    read_user_attempts,
    read_user_review_options,
    with_release,
)
from vektra_assess.moodle.result import Readable, Reading, Unreadable, UnreadableReason

log = structlog.get_logger(__name__)

OK = "OK"
MISSING = "MISSING"
UNVERIFIABLE = "UNVERIFIABLE"

DENIED_FUNCTION = "core_course_get_categories"
TOKEN_REJECTED_EVENT = "assess_ws_token_rejected"
#: a rejection is only "revoked" when Moodle itself answered; these are local refusals
_NOT_A_REJECTION = frozenset({"no_token", "egress_denied"})

_NAME = re.compile(r"[a-z][a-z0-9_]{1,48}")
_CODE = re.compile(r"[a-z0-9_]{1,64}")


@dataclass(frozen=True)
class ItemResult:
    name: str
    result: str  # OK | MISSING | UNVERIFIABLE
    reason_code: str | None = None

    def as_json(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "result": self.result,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class Outcome:
    """What one item decided (the registry adds the name)."""

    result: str
    reason_code: str | None = None


def ok(reason: str | None = None) -> Outcome:
    return Outcome(OK, reason)


def missing(reason: str) -> Outcome:
    return Outcome(MISSING, reason)


def unverifiable(reason: str) -> Outcome:
    return Outcome(UNVERIFIABLE, reason)


@dataclass(frozen=True)
class CheckContext:
    client: MoodleWsClient
    site: SiteRef  # carries the release when site_info answered
    settings: VektraAssessSettings
    manifest: Mapping[str, Any] | None  # the probe manifest of the site
    info: Reading[SiteInfo]  # site_info, read once for the whole run


ItemCheck = Callable[[CheckContext], Awaitable[Outcome]]

#: the plans that own these register them; until then they are MISSING (fail closed)
PLUGGABLE_ITEMS: tuple[str, ...] = (
    "parser_sidecar_running",
    "smtp_probe",
    "db_log_statement_none",
)


class ItemRegistry:
    """Item name -> async check. Order of registration is the order of the report."""

    def __init__(self) -> None:
        self._items: dict[str, ItemCheck] = {}

    def register(self, name: str, check: ItemCheck) -> None:
        if not _NAME.fullmatch(name):
            raise ValueError("item names are [a-z][a-z0-9_]{1,48}")
        if name in self._items:
            raise ValueError(f"item {name!r} is already registered")
        self._items[name] = check

    def get(self, name: str) -> ItemCheck | None:
        return self._items.get(name)

    def names(self) -> list[str]:
        return list(self._items)


def _code(value: str, default: str = "other") -> str:
    v = value.lower()
    return v if _CODE.fullmatch(v) else default


def _unreadable_outcome(u: Unreadable) -> Outcome:
    return unverifiable(u.reason.value)


# --- built-in items -------------------------------------------------------------------------------


def _info(ctx: CheckContext) -> SiteInfo | Outcome:
    """The site_info, or the outcome every WS-dependent item reports without calling."""
    if isinstance(ctx.info, Readable):
        return ctx.info.value
    if ctx.info.reason is UnreadableReason.ACCESS_EXCEPTION:
        return missing("token_works_failed")
    return unverifiable("site_unreachable")


async def token_works(ctx: CheckContext) -> Outcome:
    if isinstance(ctx.info, Readable):
        return ok()
    code = _code(ctx.info.detail_code, ctx.info.reason.value)
    if ctx.info.reason is UnreadableReason.ACCESS_EXCEPTION:
        return missing(code)
    return unverifiable(ctx.info.reason.value)


async def interactive_login_disabled(ctx: CheckContext) -> Outcome:
    info = _info(ctx)
    if isinstance(info, Outcome):
        return info
    auth = await read_own_auth(ctx.client, ctx.site, info.userid)
    if isinstance(auth, Unreadable):
        return _unreadable_outcome(auth)
    if auth.value == "webservice":
        return ok()
    return missing(_code(f"auth_{auth.value}", "auth_other"))


async def not_site_admin(ctx: CheckContext) -> Outcome:
    info = _info(ctx)
    if isinstance(info, Outcome):
        return info
    return missing("site_admin") if info.user_is_site_admin else ok()


async def not_ingestion_account(ctx: CheckContext) -> Outcome:
    info = _info(ctx)
    if isinstance(info, Outcome):
        return info
    ingest = ctx.settings.ingest_account_username
    if not ingest:
        return unverifiable("ingest_username_unset")
    if info.username.casefold() == ingest.casefold():
        return missing("is_ingestion_account")
    return ok()


async def denied_function_probe(ctx: CheckContext) -> Outcome:
    """Calls a read-only function that is NOT in the service: it must be refused."""
    info = _info(ctx)
    if isinstance(info, Outcome):
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


async def no_grade_write(ctx: CheckContext) -> Outcome:
    info = _info(ctx)
    if isinstance(info, Outcome):
        return info
    if any(is_grade_write_function(f) for f in info.functions):
        return missing("grade_write_function_in_service")
    return ok()


async def ws_tls_ok(ctx: CheckContext) -> Outcome:
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


def _first_failure(readings: Sequence[Reading[Any]]) -> Outcome | None:
    for r in readings:
        if isinstance(r, Unreadable):
            return missing(f"unreadable_{r.reason.value}")
    return None


def _reads(ctx: CheckContext, group: str) -> Mapping[str, Any] | Outcome:
    info = _info(ctx)
    if isinstance(info, Outcome):
        return info
    args = (ctx.manifest or {}).get("required_reads", {}).get(group)
    if not args:
        return unverifiable("no_probe_manifest")
    return {**args, "courseid": int((ctx.manifest or {}).get("courseid", 0))}


async def required_reads_quiz(ctx: CheckContext) -> Outcome:
    a = _reads(ctx, "quiz")
    if isinstance(a, Outcome):
        return a
    c, s = ctx.client, ctx.site
    readings: list[Reading[Any]] = [
        await read_quiz_close_inputs(c, s, a["courseid"], a["quizid"]),
        await read_user_attempts(c, s, a["quizid"], a["userid"]),
        await read_attempt_review(c, s, a["attemptid"]),
        await read_user_review_options(c, s, a["quizid"], a["userid"]),
    ]
    return _first_failure(readings) or ok()


async def required_reads_assign(ctx: CheckContext) -> Outcome:
    a = _reads(ctx, "assign")
    if isinstance(a, Outcome):
        return a
    c, s = ctx.client, ctx.site
    readings: list[Reading[Any]] = [
        await read_assign_settings(c, s, a["courseid"], a["assignid"]),
        await read_assign_close_inputs(c, s, a["assignid"], a["userid"]),
        await read_submission_status(c, s, a["assignid"], a["userid"]),
    ]
    return _first_failure(readings) or ok()


async def required_reads_grade(ctx: CheckContext) -> Outcome:
    a = _reads(ctx, "grade")
    if isinstance(a, Outcome):
        return a
    res = await read_grade_release(
        ctx.client, ctx.site, a["courseid"], a["userid"], a["cmid"]
    )
    return _first_failure([res]) or ok()


async def required_reads_grading(ctx: CheckContext) -> Outcome:
    a = _reads(ctx, "grading")
    if isinstance(a, Outcome):
        return a
    readings: list[Reading[Any]] = [
        await read_grading_view(ctx.client, ctx.site, a["cmid"]),
        await read_rubric_fillings(ctx.client, ctx.site, a["definitionid"]),
    ]
    return _first_failure(readings) or ok()


async def required_reads_enrol(ctx: CheckContext) -> Outcome:
    a = _reads(ctx, "enrol")
    if isinstance(a, Outcome):
        return a
    readings: list[Reading[Any]] = [
        await read_enrolment(ctx.client, ctx.site, a["courseid"], a["userid"]),
        await read_graders(ctx.client, ctx.site, a["courseid"]),
    ]
    return _first_failure(readings) or ok()


async def required_reads_files(ctx: CheckContext) -> Outcome:
    """The service may download files and the smallest file of the probe submission arrives
    with the size Moodle announced."""
    a = _reads(ctx, "files")
    if isinstance(a, Outcome):
        return a
    info = _info(ctx)
    assert not isinstance(info, Outcome)
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


# --- ui_not_same_origin ---------------------------------------------------------------------------


def _host_of(origin: str) -> str | None:
    parts = urlsplit(origin.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.hostname.lower().rstrip(".")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _site_key(host: str) -> str:
    """An approximation of the registrable domain WITHOUT the public-suffix list: the last two
    labels (the whole host for an IP literal or a single label). It can only err towards
    "same site": ``a.example.co.uk`` and ``b.example.co.uk`` end in ``co.uk`` here although a
    browser treats them as different sites. The check refuses on a match, so the error is a
    false refusal that an operator can read and fix, never an accepted same-site deployment."""
    if _is_ip(host):
        return host
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def same_site(a: str, b: str) -> bool:
    """Same host, one a subdomain of the other, or the same (approximate) registrable domain."""
    ha, hb = _host_of(a), _host_of(b)
    if ha is None or hb is None:
        raise ValueError("not an http(s) origin")
    if ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha):
        return True
    return _site_key(ha) == _site_key(hb)


async def ui_not_same_origin(ctx: CheckContext) -> Outcome:
    ui = ctx.settings.ui_origin
    if not ui:
        return unverifiable("ui_origin_unset")
    try:
        if same_site(ui, ctx.site.base_url):
            return missing("same_site_as_moodle")
        api = ctx.settings.api_public_origin
        if not api:
            return unverifiable("api_origin_unset")
        if same_site(ui, api):
            return missing("same_site_as_api")
    except ValueError:
        return unverifiable("origin_invalid")
    return ok()


async def ui_origin_distinct(ctx: CheckContext) -> Outcome:
    """Plan 9 (ARCH-076): the discussion UI origin is https (outside development) and is not
    this site's Moodle origin, the precondition of the iframe's ``allow-same-origin``
    sandbox. Same decision as the API's startup refusal (``ui.origins``)."""
    from vektra_assess.ui.origins import ui_origin_problem, url_origin

    moodle = url_origin(ctx.site.base_url)
    problem = ui_origin_problem(
        ctx.settings.ui_origin,
        [moodle] if moodle else [],
        development=ctx.settings.env == "development",
    )
    return ok() if problem is None else missing(problem)


# --- registry -------------------------------------------------------------------------------------


def builtin_registry() -> ItemRegistry:
    reg = ItemRegistry()
    for name, check in (
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
        ("ui_not_same_origin", ui_not_same_origin),
        ("ui_origin_distinct", ui_origin_distinct),  # plan 9
    ):
        reg.register(name, check)
    return reg


#: the registry the worker uses; other plans call ``DEFAULT_REGISTRY.register(...)``
DEFAULT_REGISTRY = builtin_registry()

BUILTIN_ITEMS: tuple[str, ...] = tuple(DEFAULT_REGISTRY.names())


async def run_items(
    ctx: CheckContext, registry: ItemRegistry | None = None
) -> tuple[ItemResult, ...]:
    """Every built-in and pluggable item, in a fixed order; a missing or raising item fails
    closed (``MISSING/not_registered``, ``UNVERIFIABLE/check_error``)."""
    reg = registry or DEFAULT_REGISTRY
    names = [*BUILTIN_ITEMS, *PLUGGABLE_ITEMS]
    names += [n for n in reg.names() if n not in names]
    out: list[ItemResult] = []
    for name in names:
        check = reg.get(name)
        if check is None:
            out.append(ItemResult(name, MISSING, "not_registered"))
            continue
        try:
            outcome = await check(ctx)
        except Exception as exc:
            log.warning(
                "setup_check_item_error", item=name, error_type=type(exc).__name__
            )
            outcome = unverifiable("check_error")
        if outcome.result not in (OK, MISSING, UNVERIFIABLE):
            outcome = unverifiable("check_error")
        out.append(ItemResult(name, outcome.result, outcome.reason_code))
    return tuple(out)


# --- a run ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckOutcome:
    items: tuple[ItemResult, ...]
    release: str | None  # what site_info reported; None when it did not answer
    flags: tuple[FlagResult, ...]  # empty when the site did not answer
    token_rejected: bool
    token_reason: str | None = None

    @property
    def all_ok(self) -> bool:
        return all(i.result == OK for i in self.items)


async def evaluate(
    client: MoodleWsClient,
    site: SiteRef,
    settings: VektraAssessSettings,
    manifest: Mapping[str, Any] | None,
    *,
    registry: ItemRegistry | None = None,
    now: int | None = None,
    probe: bool = True,
) -> CheckOutcome:
    """Network only: no database. Reads site_info once, runs the items, then the probes."""
    info = await read_site_info(client, site)
    release: str | None = None
    ref = site
    if isinstance(info, Readable):
        release = info.value.release
        ref = with_release(site, info.value)
    ctx = CheckContext(client, ref, settings, manifest, info)
    items = await run_items(ctx, registry)
    flags: tuple[FlagResult, ...] = ()
    if probe and release is not None:
        flags = await run_probes(client, ref, manifest, now=now)
    rejected = (
        isinstance(info, Unreadable)
        and info.reason is UnreadableReason.ACCESS_EXCEPTION
        and info.detail_code not in _NOT_A_REJECTION
    )
    reason = (
        _code(info.detail_code, "access_exception")
        if isinstance(info, Unreadable) and rejected
        else None
    )
    return CheckOutcome(items, release, flags, rejected, reason)


async def run_setup_check(
    engine: AsyncEngine,
    *,
    site_id: UUID,
    client: MoodleWsClient,
    settings: VektraAssessSettings,
    registry: ItemRegistry | None = None,
    report_id: UUID | None = None,
    now: int | None = None,
) -> CheckOutcome:
    """One check run as the worker would do it (the worker handler of plan 3 calls this).

    1. mark the report ``running`` (a queued report is re-validated, an unknown or finished one
       is refused) in its own transaction;
    2. network: site_info, items, probes;
    3. ONE transaction: ``observe_release`` first, then every capability row, the item rows, the
       audit event of a rejected token, and the report ``done``. A crash anywhere in it leaves
       no partial matrix; the report is then marked ``failed`` and the error re-raised.
    """
    async with engine.begin() as conn:
        site_row = (
            await conn.execute(
                text(
                    "SELECT base_url, probe_manifest, last_seen_release FROM assess_sites "
                    "WHERE id = :s"
                ),
                {"s": site_id},
            )
        ).one_or_none()
        if site_row is None:
            raise LookupError("unknown site")
        if report_id is None:
            report_id = (
                await conn.execute(
                    text(
                        "INSERT INTO assess_setup_check_reports (site_id, status) "
                        "VALUES (:s, 'running') RETURNING id"
                    ),
                    {"s": site_id},
                )
            ).scalar_one()
        else:
            claimed = (
                await conn.execute(
                    text(
                        "UPDATE assess_setup_check_reports SET status = 'running' "
                        "WHERE id = :r AND site_id = :s AND status IN ('queued', 'running')"
                    ),
                    {"r": report_id, "s": site_id},
                )
            ).rowcount
            if claimed != 1:
                raise LookupError("setup-check report is not runnable")
    base_url, manifest, last_seen = site_row

    try:
        outcome = await evaluate(
            client,
            SiteRef(str(site_id), base_url),
            settings,
            manifest,
            registry=registry,
            now=now,
        )
        async with engine.begin() as conn:
            release = outcome.release or last_seen
            if outcome.release is not None:
                await observe_release(conn, site_id, outcome.release)
                if outcome.flags:
                    await persist_matrix(conn, site_id, outcome.release, outcome.flags)
            for item in outcome.items:
                await conn.execute(
                    text(
                        "INSERT INTO assess_setup_checks (site_id, item, result) "
                        "VALUES (:s, :i, :r)"
                    ),
                    # UNVERIFIABLE is stored as 'missing': the table only knows ok | missing
                    {
                        "s": site_id,
                        "i": item.name,
                        "r": "ok" if item.result == OK else "missing",
                    },
                )
            if outcome.token_rejected:
                await conn.execute(
                    text(
                        "INSERT INTO assess_audit_events "
                        "(site_id, event, reason_code, actor_kind) "
                        "VALUES (:s, :e, :c, 'worker')"
                    ),
                    {
                        "s": site_id,
                        "e": TOKEN_REJECTED_EVENT,
                        "c": outcome.token_reason,
                    },
                )
            await conn.execute(
                text(
                    "UPDATE assess_setup_check_reports SET status = 'done', "
                    "moodle_release = :rel, items = CAST(:items AS jsonb), "
                    "finished_at = now() WHERE id = :r"
                ),
                {
                    "rel": release,
                    "items": json.dumps([i.as_json() for i in outcome.items]),
                    "r": report_id,
                },
            )
    except BaseException as exc:
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE assess_setup_check_reports SET status = 'failed', "
                        "finished_at = now() WHERE id = :r AND status = 'running'"
                    ),
                    {"r": report_id},
                )
        finally:
            log.error(
                "setup_check_failed", site_id=str(site_id), error=type(exc).__name__
            )
        raise
    if outcome.token_rejected:
        # operator alert: metadata only (never the token, never a URL)
        log.error(
            "assess_ws_token_rejected",
            alert="operator",
            site_id=str(site_id),
            reason_code=outcome.token_reason,
        )
    log.info(
        "setup_check_done",
        site_id=str(site_id),
        ok=sum(1 for i in outcome.items if i.result == OK),
        not_ok=sum(1 for i in outcome.items if i.result != OK),
    )
    return outcome

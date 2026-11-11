"""One setup-check run (REQ-079, ADR-0033 D3): the provider's items, Vektra's items, the
capability probes, and the report lifecycle.

The provider supplies three things, each through the ``LmsProvider`` Protocol:

- ``site_info``: the release, and whether the service token was rejected;
- ``service_account_check``: its declared items (``setup_items``). A declared item it does not
  return is ``MISSING/not_returned``, the same fail-closed rule as ``not_registered``;
- ``probe_capabilities``: one result per flag of ``capabilities.FLAGS``. A flag it does not
  return is recorded ``unreadable/not_probed``, which fails closed.

Every provider call is wrapped: an exception that escapes it is an ``Unreadable`` or an
``UNVERIFIABLE/check_error``, never a crashed run (ADR-0033 D2, invariant 1).

Revoked token: ``Unreadable(access_exception)`` from ``site_info`` (and not a local refusal)
makes the site NotReady, writes the audit event ``assess_ws_token_rejected`` in the SAME
transaction as the report, and emits one operator alert log event (metadata only, no token).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from vektra_assess.capabilities import FLAGS, observe_release
from vektra_assess.config import VektraAssessSettings
from vektra_assess.setup.items import DEFAULT_REGISTRY, GENERIC_ITEMS, PLUGGABLE_ITEMS
from vektra_assess.setup.registry import (
    MISSING,
    NAME,
    OK,
    UNVERIFIABLE,
    CheckContext,
    ItemRegistry,
    ItemResult,
    SetupCheckStale,
    reason_code,
    unverifiable,
)
from vektra_shared.lms import (
    CapabilityResult,
    LmsProvider,
    LmsSite,
    LmsSiteInfo,
    Readable,
    Reading,
    Unreadable,
    UnreadableReason,
    guarded,
)

log = structlog.get_logger(__name__)

TOKEN_REJECTED_EVENT = "assess_ws_token_rejected"
#: a rejection is only "revoked" when the LMS itself answered; these are local refusals
_NOT_A_REJECTION = frozenset({"no_token", "egress_denied"})
_STATES = frozenset({"readable", "partial", "unreadable"})


# --- items ----------------------------------------------------------------------------------------


def _declared(
    provider: LmsProvider | None, reserved: set[str]
) -> tuple[str, ...] | None:
    """The provider's declared item names, or None when they cannot be trusted (an invalid or
    reserved name, a duplicate, a declaration the guard could not read): the run then fails
    closed."""
    if provider is None:
        return ()
    names = tuple(provider.setup_items)
    if len(set(names)) != len(names) or any(
        not isinstance(n, str) or not NAME.fullmatch(n) or n in reserved for n in names
    ):
        return None
    return names


async def _provider_items(
    provider: LmsProvider,
    names: tuple[str, ...],
    ctx: CheckContext,
) -> list[ItemResult]:
    # behind the guard: a raise is every declared item UNVERIFIABLE/check_error
    returned = await provider.service_account_check(ctx.site, ctx.info, ctx.manifest)
    by_name: dict[str, ItemResult] = {}
    for item in returned:
        if item.name in names and item.name not in by_name:
            result = item.result if item.result in (OK, MISSING, UNVERIFIABLE) else None
            by_name[item.name] = (
                ItemResult(item.name, result, item.reason_code)
                if result is not None
                else ItemResult(item.name, UNVERIFIABLE, "check_error")
            )
    undeclared = sorted({i.name for i in returned} - set(names))
    if undeclared:
        log.warning("setup_check_undeclared_items", count=len(undeclared))
    return [by_name.get(n) or ItemResult(n, MISSING, "not_returned") for n in names]


async def run_items(
    ctx: CheckContext, registry: ItemRegistry | None = None
) -> tuple[ItemResult, ...]:
    """The provider's declared items, then Vektra's built-in and pluggable items, in a fixed
    order; a missing or raising item fails closed (``MISSING/not_registered``,
    ``MISSING/not_returned``, ``UNVERIFIABLE/check_error``)."""
    reg = registry or DEFAULT_REGISTRY
    vektra = [*GENERIC_ITEMS, *PLUGGABLE_ITEMS]
    vektra += [n for n in reg.names() if n not in vektra]
    out: list[ItemResult] = []
    declared = _declared(ctx.provider, set(vektra))
    if ctx.provider is not None and declared is None:
        # a provider whose item list cannot be trusted vouches for nothing: not registered
        ctx = replace(ctx, provider=None)
        declared = ()
    elif ctx.provider is not None:
        assert declared is not None
        out += await _provider_items(ctx.provider, declared, ctx)
    for name in vektra:
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


# --- capability probes ----------------------------------------------------------------------------


def _not_probed(flag: str, release: str, reason: str) -> CapabilityResult:
    evidence = hashlib.sha256(f"{flag}\n{release}\n{reason}".encode()).hexdigest()
    return CapabilityResult(flag, "unreadable", reason, evidence)


async def probe(
    provider: LmsProvider,
    site: LmsSite,
    manifest: Mapping[str, Any] | None,
    flags: Sequence[str] = FLAGS,
) -> tuple[CapabilityResult, ...]:
    """One result per flag, in ``flags`` order: what the provider returned for it, or
    ``unreadable/not_probed`` when it returned nothing usable (fail closed). A provider that
    raised returns ``not_probed`` for every flag (the guard), so the site's release does not
    move on a matrix that probed nothing."""
    release = site.release or ""
    returned = await guarded(provider).probe_capabilities(site, manifest, tuple(flags))
    by_flag: dict[str, CapabilityResult] = {}
    for r in returned:
        if r.flag in flags and r.flag not in by_flag and r.state in _STATES:
            by_flag[r.flag] = r
    return tuple(
        by_flag.get(f) or _not_probed(f, release, UnreadableReason.NOT_PROBED.value)
        for f in flags
    )


async def persist_matrix(
    conn: AsyncConnection,
    site_id: Any,
    release: str,
    results: tuple[CapabilityResult, ...],
) -> None:
    """Write every flag row. The CALLER owns the transaction (the whole check run is one):
    all rows or none. ``release`` is the one ``observe_release`` recorded.

    ``assess_sites.release`` is "the release whose capability matrix is valid" (coordinator
    decision 2026-11-08): the render check and the pre-flight pick their reader model set from
    it. It moves to ``release`` here, in the same transaction, only when at least one flag was
    actually probed: a matrix of ``not_probed`` rows (no probe manifest) vouches for nothing.
    """
    probed = any(r.reason_code != UnreadableReason.NOT_PROBED.value for r in results)
    if probed:
        await conn.execute(
            text("UPDATE assess_sites SET release = :r WHERE id = :s"),
            {"r": release, "s": site_id},
        )
    for r in results:
        await conn.execute(
            text(
                "INSERT INTO assess_site_capabilities "
                "(site_id, flag, state, release, reason_code, evidence_hash, checked_at) "
                "VALUES (:s, :f, :st, :rel, :rc, :eh, now()) "
                "ON CONFLICT (site_id, flag) DO UPDATE SET state = EXCLUDED.state, "
                "release = EXCLUDED.release, reason_code = EXCLUDED.reason_code, "
                "evidence_hash = EXCLUDED.evidence_hash, checked_at = now()"
            ),
            {
                "s": site_id,
                "f": r.flag,
                "st": r.state,
                "rel": release,
                "rc": r.reason_code,
                "eh": r.evidence_hash,
            },
        )


# --- a run ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckOutcome:
    items: tuple[ItemResult, ...]
    release: str | None  # what site_info reported; None when it did not answer
    flags: tuple[CapabilityResult, ...]  # empty when the site did not answer
    token_rejected: bool
    token_reason: str | None = None

    @property
    def all_ok(self) -> bool:
        return all(i.result == OK for i in self.items)


async def _site_info(
    provider: LmsProvider | None, site: LmsSite
) -> Reading[LmsSiteInfo]:
    if provider is None:
        return Unreadable(UnreadableReason.NOT_PROBED, "no_provider")
    return await provider.site_info(site)


async def evaluate(
    provider: LmsProvider | None,
    site: LmsSite,
    settings: VektraAssessSettings,
    manifest: Mapping[str, Any] | None,
    *,
    registry: ItemRegistry | None = None,
    probe_flags: bool = True,
    moodle_origin: str | None = None,
) -> CheckOutcome:
    """Network only: no database. Reads site_info once, runs the items, then the probes.

    The provider is used behind ``guarded`` (the one fail-closed wrapper; a no-op when the
    composition already wrapped it): no provider exception reaches this run."""
    provider = None if provider is None else guarded(provider)
    info = await _site_info(provider, site)
    release: str | None = None
    ref = site
    if isinstance(info, Readable):
        release = info.value.release
        ref = replace(site, release=release)
    ctx = CheckContext(ref, settings, manifest, info, moodle_origin, provider)
    items = await run_items(ctx, registry)
    flags: tuple[CapabilityResult, ...] = ()
    if probe_flags and release is not None and provider is not None:
        flags = await probe(provider, ref, manifest)
    rejected = (
        isinstance(info, Unreadable)
        and info.reason is UnreadableReason.ACCESS_EXCEPTION
        and info.detail_code not in _NOT_A_REJECTION
    )
    reason = (
        reason_code(info.detail_code, "access_exception")
        if isinstance(info, Unreadable) and rejected
        else None
    )
    return CheckOutcome(items, release, flags, rejected, reason)


async def run_setup_check(
    engine: AsyncEngine,
    *,
    site_id: UUID,
    provider: LmsProvider | None,
    settings: VektraAssessSettings,
    registry: ItemRegistry | None = None,
    report_id: UUID | None = None,
) -> CheckOutcome:
    """One check run as the worker does it (the ``setup_check`` handler calls this).

    1. mark the report ``running`` (a queued report is re-validated, an unknown or finished one
       is refused) in its own transaction;
    2. network: site_info, items, probes;
    3. ONE transaction: ``observe_release`` first, then every capability row with the site's
       ``release`` (only when the probes ran), the item rows, the
       audit event of a rejected token, and the report ``done``. A crash anywhere in it leaves
       no partial matrix; the report is then marked ``failed`` and the error re-raised.
    """
    async with engine.begin() as conn:
        site_row = (
            await conn.execute(
                text(
                    "SELECT base_url, probe_manifest, last_seen_release, moodle_origin "
                    "FROM assess_sites "
                    "WHERE id = :s"
                ),
                {"s": site_id},
            )
        ).one_or_none()
        if site_row is None:
            raise SetupCheckStale("unknown_site")
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
                raise SetupCheckStale("report_not_runnable")
    base_url, manifest, last_seen, framing_origin = site_row

    try:
        outcome = await evaluate(
            provider,
            LmsSite(str(site_id), base_url),
            settings,
            manifest,
            registry=registry,
            moodle_origin=framing_origin,
        )
        async with engine.begin() as conn:
            # a site disabled while the network part ran gets nothing written:
            # FOR UPDATE holds off a concurrent disable until this commits, and a
            # disable that committed first is seen here. FOR UPDATE, not FOR SHARE: it is the
            # lock observe_release asks for, taken first so two persists of one site serialise
            # instead of deadlocking
            if (await conn.execute(_SITE_ENABLED, {"s": site_id})).scalar() is not True:
                await conn.execute(_FAIL_RUNNING, {"r": report_id})
                abandoned = True
            else:
                abandoned = False
                await _persist(conn, site_id, report_id, outcome, last_seen)
        if abandoned:
            raise SetupCheckStale("site_disabled")
    except SetupCheckStale:
        raise  # not a failure: the caller logs it once, with its reason_code
    except BaseException as exc:
        try:
            async with engine.begin() as conn:
                await conn.execute(_FAIL_RUNNING, {"r": report_id})
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


_SITE_ENABLED = text(
    "SELECT disabled_at IS NULL FROM assess_sites WHERE id = :s FOR UPDATE"
)
_FAIL_RUNNING = text(
    "UPDATE assess_setup_check_reports SET status = 'failed', "
    "finished_at = now() WHERE id = :r AND status = 'running'"
)


async def _persist(
    conn: Any, site_id: UUID, report_id: Any, outcome: CheckOutcome, last_seen: Any
) -> None:
    """Step 3 of ``run_setup_check``: everything a run writes, in the caller's transaction."""
    release = outcome.release or last_seen
    if outcome.release is not None:
        await observe_release(conn, site_id, outcome.release)
        if outcome.flags:
            # also moves ``assess_sites.release`` when the matrix was probed
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

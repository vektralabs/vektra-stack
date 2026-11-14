"""Composition root of the assess worker process (plan assess-foundations 6.2, COMP-003).

``docker/entrypoint.sh assess-worker`` runs ``python -m vektra_app.assess_worker``. The
worker is the only process holding the KEK, the WS tokens and the SMTP account, and it
has no listener. Before doing anything it proves its own boundaries, and exits non-zero
if any check fails:

1. assess is enabled (a dormant deployment must not run the worker at all);
2. the key matrix for the worker role (required present, forbidden absent);
3. the crypto self-test (no weaker fallback, ADR-0031);
4. its DSN is its own role, ``vektra_assess_worker``, and not a superuser;
5. the import ceiling: none of the ML stack, the vector store client or the ingest and
   index components is loaded (ARCH-073), measured after ``LitellmProvider`` is imported
   so its cost is part of the idle footprint.

Then it composes the job loop (plan assess-job-loop): policy and slot table, the control
connection with its NOTIFY self-test, startup reconciliation, the heartbeat, the claim loop and
the ticks, and runs until SIGTERM/SIGINT, when it drains, releases its leases and exits 0.
Production slots that have no handler yet make it refuse to start; only in development
(``VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND=true``) it starts anyway, and a job of an unbound kind
then fails with ``handler_not_bound`` instead of completing silently.

This module must not import ``vektra_app.main``: that builds the whole API.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import socket
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from importlib import metadata
from typing import Any

import structlog

from vektra_shared.startup import StartupValidationError

log = structlog.get_logger(__name__)

STEP = "assess_worker_check"
WORKER_ROLE = "vektra_assess_worker"
APPLICATION_NAME = "vektra-assess-worker"
# ``fastapi`` is deliberately not listed: ``litellm`` (the LLM provider the worker needs) imports
# ``fastapi.exceptions`` itself, so it is always present with it. What ARCH-073 keeps out is the
# Vektra API stack (``vektra_app.main``, ``vektra_learn``, ``uvicorn``), checked here and in the
# fresh-process test.
FORBIDDEN_MODULES: tuple[str, ...] = (
    "sentence_transformers",
    "torch",
    "transformers",
    "qdrant_client",
    "vektra_index",
    "vektra_ingest",
    "vektra_learn",
    "uvicorn",
    "presidio_analyzer",
    "presidio_anonymizer",
    "spacy",
    "unstructured",
)


class DisabledError(RuntimeError):
    """VEKTRA_ASSESS_ENABLED is false: the worker refuses to run."""


def assert_import_ceiling(loaded: Iterable[str] | None = None) -> None:
    modules = set(sys.modules if loaded is None else loaded)
    present = sorted(m for m in FORBIDDEN_MODULES if m in modules)
    if present:
        raise StartupValidationError(
            step=STEP,
            detail=f"forbidden modules loaded in the assess worker: {present}",
            remediation=(
                "The worker must not import the ML stack or other components "
                "(ARCH-073). Find the import chain with `python -X importtime`."
            ),
        )


def assert_smtp_configured(settings: Any) -> None:
    """Transcripts and notifications are delivered by the worker's SMTP (REQ-075, REQ-071):
    without a relay, a From address and the operator's address the delivery slots stay unbound,
    so the worker refuses to start, naming the settings, unless the development override lets
    it run with unbound slots."""
    if settings.worker_allow_unbound:
        return
    if not (settings.smtp_host and settings.smtp_from):
        raise StartupValidationError(
            step=STEP,
            detail="VEKTRA_ASSESS_SMTP_HOST and VEKTRA_ASSESS_SMTP_FROM must be set: "
            "transcripts are delivered by the worker's SMTP (REQ-075)",
            remediation=(
                "Set the relay host and the From address (and the assess_smtp secret with the "
                "account), or for development only VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND=true with "
                "VEKTRA_ENV=development."
            ),
        )
    if not settings.operator_email:
        # the operator alerts (REQ-071) go to this address: without it the operator_notify
        # slot stays unbound, so the worker is refused here, before the database, by name
        raise StartupValidationError(
            step=STEP,
            detail="VEKTRA_ASSESS_OPERATOR_EMAIL must be set: the operator alerts (REQ-071) "
            "are mailed to it through the same relay",
            remediation=(
                "Set VEKTRA_ASSESS_OPERATOR_EMAIL to the platform operator's address, or for "
                "development only VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND=true with "
                "VEKTRA_ENV=development."
            ),
        )


#: the heartbeat, the lease reaper and the STARTING reaper get their own engine of this size,
#: carved out of ``VEKTRA_ASSESS_WORKER_POOL_SIZE`` (plan 13a review): job work can exhaust the
#: job pool without ever delaying a beat, and the INT-002 budget (pool + control) is unchanged
LIVENESS_POOL_SIZE = 1
LIVENESS_APPLICATION_NAME = "vektra-assess-worker-liveness"


def split_pool(pool_size: int) -> tuple[int, int]:
    """``(job pool, liveness pool)`` for the configured worker pool. A pool of 1 cannot be split:
    the job pool keeps its 1 and the liveness engine adds 1 (the only case above the budget)."""
    return max(1, pool_size - LIVENESS_POOL_SIZE), LIVENESS_POOL_SIZE


def make_engines(
    url: str, pool_size: int, command_timeout_s: float = 30.0
) -> tuple[Any, Any]:
    """The job engine and the liveness engine, together the configured pool size."""
    jobs, live = split_pool(pool_size)
    return (
        make_engine(url, jobs, command_timeout_s),
        make_engine(
            url,
            live,
            command_timeout_s,
            application_name=LIVENESS_APPLICATION_NAME,
            pool_timeout=5,
        ),
    )


def make_engine(
    url: str,
    pool_size: int,
    command_timeout_s: float = 30.0,
    *,
    application_name: str = APPLICATION_NAME,
    pool_timeout: float = 15,
) -> Any:
    """The worker's single pool: it serves the job SQL and the handlers' transactions.

    Pool plus the control connection is the 5 + 1 of the connection budget (INT-002): no
    overflow, and a bounded wait for a free connection instead of opening a sixth one. The
    pools allow more concurrent jobs than there are connections (a handler holds one only while it
    runs a statement), so a handler may queue here; 15 s is long enough to ride out a burst and
    short enough to fail inside a lease.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=0,
        pool_timeout=pool_timeout,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {"application_name": application_name},
            # a half-open connection (database restart, dropped network) answers nothing and
            # raises nothing: without these bounds every worker call would wait on it forever
            "timeout": 5,
            "command_timeout": command_timeout_s,
        },
    )


@dataclass(frozen=True, slots=True)
class Checked:
    engine: Any  # AsyncEngine
    settings: Any  # VektraAssessSettings
    worker_url: str
    keys: Mapping[str, Any] = field(default_factory=dict)  # the loaded worker secrets
    #: the heartbeat's and the reapers' own engine; None (tests composing without it) means
    #: they share the job engine
    liveness: Any = None
    #: the ONE LMS provider of this worker (ADR-0033 D2), loaded by entry point from
    #: ``VEKTRA_ASSESS_LMS_PROVIDER``: one rate cap, one breaker registry and one connection
    #: pool serve every caller. None only in a composition test that loads none.
    lms: Any = None

    @property
    def liveness_engine(self) -> Any:
        return self.liveness if self.liveness is not None else self.engine


async def run_checks() -> object:
    """Run checks 1-5. Returns the worker engine (an AsyncEngine)."""
    return (await _checks()).engine


def _find_lms(settings: Any, entry_points: Any | None = None) -> Any:
    """The configured provider's entry-point object (ADR-0033 D2), loaded but NOT called, or a
    refusal naming the setting. Unset, unknown or broken is a ``StartupValidationError``: with
    assess enabled and no provider the vertical is unavailable (ADR-0033 D1). A test double is
    found only with ``VEKTRA_ENV=development`` set explicitly."""
    from vektra_app.lms_providers import find_lms_provider

    return find_lms_provider(
        settings.lms_provider,
        entry_points=entry_points,
        allow_test_doubles=settings.is_development,
    )


async def _build_lms(settings: Any, factory: Any) -> Any:
    """The provider built from ``factory`` with the resolved development flag, behind the one
    fail-closed wrapper; its own startup problems and an unusable call deadline refuse here."""
    from vektra_app.lms_providers import build_lms_provider
    from vektra_shared.lms import MAX_CALL_DEADLINE_S, StartupProblem, guarded

    # behind the one fail-closed wrapper (ADR-0033 D2, invariant 1): every caller of this
    # worker reads through it
    provider = guarded(
        await build_lms_provider(
            settings.lms_provider,
            factory,
            development=settings.is_development,
            allow_test_doubles=settings.is_development,
        )
    )
    # the resolved flag (2a-1.11): development only when VEKTRA_ENV=development is explicit
    problems = list(provider.startup_problems(settings.is_development))
    deadline = provider.call_deadline_s
    if not (math.isfinite(deadline) and 0 < deadline <= MAX_CALL_DEADLINE_S):
        problems.append(
            StartupProblem(
                setting="VEKTRA_ASSESS_LMS_PROVIDER",
                detail=(
                    f"the LMS provider {provider.name} reports call_deadline_s={deadline!r}, "
                    f"not in (0, {MAX_CALL_DEADLINE_S:g}]: no job budget can be derived"
                ),
                remediation="Fix the provider's call timeout setting.",
            )
        )
    if problems:
        with contextlib.suppress(Exception):
            await provider.aclose()
        raise StartupValidationError(
            step=STEP, detail=problems[0].detail, remediation=problems[0].remediation
        )
    return provider


async def _load_lms(settings: Any, entry_points: Any | None = None) -> Any:
    """``_find_lms`` then ``_build_lms``: the provider, built and checked."""
    return await _build_lms(settings, _find_lms(settings, entry_points))


async def _checks(lms_entry_points: Any | None = None) -> Checked:
    from pydantic import ValidationError

    from vektra_app.lms_providers import declared_secrets
    from vektra_assess.config import VektraAssessSettings, explain_settings_error
    from vektra_assess.keys import enforce_key_matrix

    try:
        settings = VektraAssessSettings()
    except ValidationError as exc:
        detail, remediation = explain_settings_error(exc)
        raise StartupValidationError(
            step=STEP, detail=detail, remediation=remediation
        ) from None
    if not settings.enabled:
        raise DisabledError(
            "VEKTRA_ASSESS_ENABLED is not true: the assess worker does not run on a "
            "deployment where assess is disabled."
        )
    # the worker calls the model too (Part B, the outage probe), with its OWN environment's key
    from vektra_app.assess_llm import check_model_credentials

    check_model_credentials(settings, process="worker")
    # Review of #207, finding 7: the key matrix runs BEFORE any provider is built, on the
    # secrets the provider declares statically (its entry point's REQUIRED_SECRETS), so no
    # provider code runs with a forbidden secret mounted, and a missing provider and the key
    # violations are reported together.
    lms_problem: StartupValidationError | None = None
    factory: Any = None
    declared: tuple[str, ...] = ()
    try:
        factory = _find_lms(settings, lms_entry_points)
        # found: the name is set (find_lms_provider refuses an unset one)
        declared = declared_secrets(str(settings.lms_provider), factory)
    except StartupValidationError as exc:
        lms_problem = exc
    try:
        loaded = enforce_key_matrix(
            "worker",
            secrets_dir=settings.secrets_dir,
            step=STEP,
            provider_secrets=declared,
        )
    except StartupValidationError as keys_problem:
        if lms_problem is None:
            raise
        raise StartupValidationError(
            step=STEP,
            detail=f"{keys_problem.detail}; {lms_problem.detail}",
            remediation=f"{keys_problem.remediation} {lms_problem.remediation}",
        ) from None
    if lms_problem is not None:
        raise lms_problem
    lms = await _build_lms(settings, factory)
    try:
        return await _checks_with_lms(settings, loaded, lms)
    except BaseException:
        # a later check refused: the provider it loaded is closed, not leaked
        with contextlib.suppress(Exception):
            await lms.aclose()
        raise


async def _checks_with_lms(settings: Any, loaded: Any, lms: Any) -> Checked:
    """Checks 2-5, after the LMS provider was loaded (``_checks`` closes it if one refuses)."""
    from vektra_assess.crypto.selftest import run_crypto_selftest
    from vektra_assess.db.roles import (
        assert_distinct_dsn,
        assert_session_role,
        dsn_with_password,
    )

    if settings.worker_allow_unbound and not settings.is_development:
        raise StartupValidationError(
            step=STEP,
            detail=(
                "VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND=true is only honoured with "
                f"VEKTRA_ENV=development (this process runs with {settings.env_described})"
            ),
            remediation=(
                "Unset VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND: a production worker must have "
                "every handler bound."
            ),
        )
    if settings.privacy_allow_unset and not settings.is_development:
        raise StartupValidationError(
            step=STEP,
            detail=(
                "VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET=true is only honoured with "
                f"VEKTRA_ENV=development (this process runs with {settings.env_described})"
            ),
            remediation=(
                "Unset it: a deployment configures the REQ-089 privacy settings (plan "
                "assess-privacy-retention) and the Start gate checks them."
            ),
        )
    run_crypto_selftest()
    if not settings.worker_database_url:
        raise StartupValidationError(
            step=STEP,
            detail="VEKTRA_ASSESS_WORKER_DATABASE_URL is unset",
            remediation="Set the DSN of the vektra_assess_worker role.",
        )
    worker_url = dsn_with_password(
        settings.worker_database_url,
        loaded["assess_db_worker_password"].value,
        step=STEP,
    )
    main_url = os.environ.get("VEKTRA_DATABASE_URL")
    if main_url:
        assert_distinct_dsn(worker_url, main_url, step=STEP)

    # Imported for their cost, so the idle footprint and the import ceiling include them: the
    # LLM provider and the discussion runner the worker composes for Part B (plan 8 pre-flight).
    import vektra_app.assess_llm  # noqa: F401
    from vektra_core.providers.litellm_provider import LitellmProvider  # noqa: F401

    assert_import_ceiling()
    assert_smtp_configured(settings)

    engine, liveness = make_engines(
        worker_url, settings.worker_pool_size, settings.worker_db_command_timeout_s
    )
    try:
        await assert_session_role(engine, WORKER_ROLE, step=STEP)
    except BaseException:
        await engine.dispose()
        await liveness.dispose()
        raise
    return Checked(engine, settings, worker_url, loaded, liveness=liveness, lms=lms)


def _asyncpg_dsn(sqlalchemy_url: str) -> str:
    """The control connection speaks asyncpg directly: drop the SQLAlchemy driver suffix."""
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _version() -> str:
    try:
        return metadata.version("vektra-app")
    except metadata.PackageNotFoundError:
        return "unknown"


#: worker secrets the session-lifecycle handlers need (all REQUIRED for the worker role)
SESSION_KEYS = (
    "assess_kek",
    "assess_handoff_public",
    "assess_teacher_config_private",
    "assess_transcript_kek",  # plan transcript-delivery: the transcript_generate handler
)


def _lms_call_deadline(checked: Checked) -> float:
    """The composed provider's call deadline, which budgets the jobs that make sequential LMS
    calls (review of #207, finding 3). Without a provider no handler that reads the LMS is
    bound (``_bound_lms``), so there is nothing to budget: zero."""
    return float(checked.lms.call_deadline_s) if checked.lms is not None else 0.0


def _bound_lms(checked: Checked) -> Any | None:
    """The worker's LMS provider when it may serve jobs: loaded, and every secret it declares
    (``required_secrets``) was loaded by the key matrix. None otherwise, and then NO handler
    that reads the LMS is bound (the pre-flight, enrolment, render, teacher gate, transcript,
    reconcile and setup-check handlers alike): ``assert_all_bound`` refuses to start."""
    lms = checked.lms
    if lms is None or not all(name in checked.keys for name in lms.required_secrets):
        return None
    return lms


def _session_handlers(
    checked: Checked, audit: Any, preflight_budget_s: float | None = None
) -> dict[Any, Any]:
    """The plan assess-session-lifecycle bodies: the ``preflight`` handler.

    Built only from a key set that passed the key matrix (``_checks``) and with the worker's
    LMS provider: without them (a composition test with a lazy engine and no secrets) nothing
    is bound, and ``assert_all_bound`` refuses to start unless the development override is set.
    Nothing connects here: the LMS provider, the parser client and the LLM provider open their
    connections on first use.
    """
    lms = _bound_lms(checked)
    if lms is None or not all(k in checked.keys for k in SESSION_KEYS):
        return {}
    from vektra_app.assess_llm import build_assess_llm, build_worker_part_b_runner
    from vektra_assess.delivery.recipients import ConfigRecipients
    from vektra_assess.jobs import JobKind
    from vektra_assess.worker.enrolment import make_recheck_handler
    from vektra_assess.worker.parser_client import ParserClient
    from vektra_assess.worker.preflight import PreflightDeps, make_preflight_handler
    from vektra_assess.worker.render import RenderDeps, make_render_handler
    from vektra_assess.worker.reply_reconcile import make_reconcile_handler
    from vektra_assess.worker.start_source import LmsStartSource
    from vektra_assess.worker.teacher_gate import make_teacher_gate_handler
    from vektra_assess.worker.transcript_gen import (
        GenerationDeps,
        make_generate_handler,
    )

    settings, engine = checked.settings, checked.engine
    runner = build_worker_part_b_runner(
        settings,
        acquire_db=engine.connect,
        audit=audit,
        llm=build_assess_llm(settings),
    )
    source = LmsStartSource(
        lms,
        ParserClient.from_settings(settings),
        engine.connect,
        teacher_private=checked.keys["assess_teacher_config_private"],
    )
    preflight = make_preflight_handler(
        PreflightDeps(
            settings=settings,
            kek=checked.keys["assess_kek"],
            handoff_public=checked.keys["assess_handoff_public"],
            source=source,
            runner=runner,
            # the budget the composed runtime enforces on a pre-flight (with any operator
            # override): the handler keeps its confirming probe inside it
            job_budget_s=preflight_budget_s,
        )
    )
    # the transcript recipients: the sealed teacher config, opened by the worker only
    recipients = ConfigRecipients(checked.keys["assess_teacher_config_private"])
    render = make_render_handler(
        RenderDeps(
            settings=settings, source=source, graders=source, recipients=recipients
        )
    )
    transcript = make_generate_handler(
        GenerationDeps(
            kek=checked.keys["assess_kek"],
            transcript_kek=checked.keys["assess_transcript_kek"],
            facts=source,
            recipients=recipients,
            tz=settings.site_timezone,
        )
    )
    return {
        JobKind.TRANSCRIPT_GENERATE: transcript,
        JobKind.PREFLIGHT: preflight,
        JobKind.RENDER_CHECK: render,
        JobKind.GATE_REFRESH: render,
        JobKind.ENROLMENT_RECHECK: make_recheck_handler(source),
        JobKind.RECONCILE_REPLY: make_reconcile_handler(
            checked.keys["assess_kek"], settings.outage_timeout_s
        ),
        # plan assess-teacher-ops: the editors / graders / enable facts of one activity
        JobKind.TEACHER_GATE: make_teacher_gate_handler(source),
    }


#: room for a delivery row's database statements (claim, read, outcome) inside the job budget
DELIVERY_DB_MARGIN_S = 15.0


@dataclass(frozen=True, slots=True)
class DeliveryParts:
    handlers: Mapping[Any, Any]
    ticks: Mapping[str, Any]


def smtp_config(settings: Any, credentials: Any) -> Any:
    """The relay settings plus the worker-only ``assess_smtp`` account ({} = no AUTH)."""
    from vektra_assess.delivery.smtp import SmtpConfig

    data = dict(getattr(credentials, "data", {}) or {})
    username, password = data.get("username"), data.get("password")
    if (username is None) != (password is None) or not all(
        v is None or (isinstance(v, str) and v) for v in (username, password)
    ):
        raise StartupValidationError(
            step=STEP,
            detail="assess_smtp must hold both 'username' and 'password' (strings) or neither",
            remediation='Write {"username": "...", "password": "..."} (or {} for a relay '
            "that takes no AUTH) into the assess_smtp secret.",
        )
    return SmtpConfig(
        host=str(settings.smtp_host),
        port=settings.smtp_port,
        sender=str(settings.smtp_from),
        tls_mode=settings.smtp_tls_mode,
        username=username,
        password=password,
        timeout_s=settings.smtp_timeout_s,
        ca_file=settings.smtp_ca_file,
        max_connections=settings.smtp_max_connections,
    )


def make_delivery_retry_tick(
    connect: Any,
    *,
    stale_after: timedelta,
    window: timedelta,
    smtp_test_lease: timedelta,
    erasure_alert_after_s: int,
) -> Callable[[], Awaitable[None]]:
    """The ``delivery_retry`` tick body. Its steps are looked up at each run (so a test can
    replace one). The erasure sweep (OQ-033) is isolated: an exception in it is logged and the
    tick goes on, so the SMTP-test reaper always runs after it."""

    async def delivery_retry() -> None:
        from vektra_assess.delivery.handler import delivery_retry_tick
        from vektra_assess.delivery.notify import rescue_dead_chains
        from vektra_assess.delivery.smtp_test import reap_interrupted_tests
        from vektra_assess.privacy.erasure import alert_overdue_erasures

        await delivery_retry_tick(connect, stale_after=stale_after, window=window)
        # plan 12b: a notification chain whose job died gets its next run back, and an SMTP
        # test whose single-attempt job died ends FAILED interrupted (review #2)
        await rescue_dead_chains(connect)
        # OQ-033: an erasure request still open past the threshold alerts the operator once
        try:
            await alert_overdue_erasures(connect, after_s=erasure_alert_after_s)
        except Exception as exc:
            log.error(
                "assess_erasure_overdue_sweep_failed",
                error_class=type(exc).__name__,
            )
        await reap_interrupted_tests(connect, stale_after=smtp_test_lease)

    return delivery_retry


def _delivery_parts(checked: Checked) -> DeliveryParts:
    """The ``transcript_deliver``, ``teacher_notify`` and ``operator_notify`` handlers and the
    ``delivery_retry`` / ``smtp_probe`` ticks.

    Bound only with the relay configured and the worker keys loaded; otherwise nothing is bound
    and ``assert_all_bound`` (or the startup check above) refuses to run without the development
    override. One ``SmtpSender`` serves the handler and the probe: one connection cap.
    """
    settings = checked.settings
    needed = ("assess_transcript_kek", "assess_teacher_config_private", "assess_smtp")
    if not (settings.smtp_host and settings.smtp_from) or not all(
        k in checked.keys for k in needed
    ):
        return DeliveryParts({}, {})
    from vektra_assess.delivery.backoff import parse_schedule
    from vektra_assess.delivery.handler import DeliveryDeps, make_deliver_handler
    from vektra_assess.delivery.notify import NotifyDeps, make_notify_handler
    from vektra_assess.delivery.probe import run_probe
    from vektra_assess.delivery.recipients import ConfigRecipients, parse_domains
    from vektra_assess.delivery.smtp import SmtpSender
    from vektra_assess.jobs import JobKind
    from vektra_assess.worker.policy import load_policy

    sender = SmtpSender(smtp_config(settings, checked.keys["assess_smtp"]))
    policy_all = load_policy(
        os.environ, lms_call_deadline_s=_lms_call_deadline(checked)
    )
    policy = policy_all.kinds[JobKind.TRANSCRIPT_DELIVER]
    lease = policy.lease_s
    # every row of a job runs concurrently: one row's worst case (bounded slot wait + send
    # timeout + QUIT bound) plus the DB statements must fit the job's hard budget
    need = sender.worst_case_s() + DELIVERY_DB_MARGIN_S
    if policy.budget_s < need:
        raise StartupValidationError(
            step=STEP,
            detail=(
                f"transcript_deliver budget {policy.budget_s:g} s is shorter than one "
                f"delivery's worst case {need:g} s (slot wait + VEKTRA_ASSESS_SMTP_TIMEOUT_S "
                "+ QUIT + database)"
            ),
            remediation=(
                "Raise VEKTRA_ASSESS_JOB_TRANSCRIPT_DELIVER_BUDGET_S (the lease follows at "
                "2x) or lower VEKTRA_ASSESS_SMTP_TIMEOUT_S."
            ),
        )
    domains = parse_domains(settings.recipient_domains)
    if not domains:
        log.warning(
            "assess_recipient_domains_empty", reason_code="every_address_refused"
        )
    # one connection budget for every fan-out handler's rows (plan 13a):
    # the job pool minus a reserve for the job loop (the heartbeat and the reapers have their own)
    from vektra_assess.delivery.budget import RowDbBudget

    row_budget = RowDbBudget.for_pool(checked.engine.pool.size())
    deps = DeliveryDeps(
        transcript_kek=checked.keys["assess_transcript_kek"],
        recipients=ConfigRecipients(checked.keys["assess_teacher_config_private"]),
        sender=sender,
        sender_address=str(settings.smtp_from),
        message_id_domain=settings.message_id_domain
        or str(settings.smtp_from).rpartition("@")[2],
        domains=domains,
        schedule=parse_schedule(settings.smtp_retry_schedule),
        window=timedelta(hours=settings.smtp_retry_window_hours),
        stale_after=timedelta(seconds=lease),
        db_budget=row_budget,
    )
    engine = checked.engine

    delivery_retry = make_delivery_retry_tick(
        engine.connect,
        stale_after=deps.stale_after,
        window=deps.window,
        smtp_test_lease=timedelta(
            seconds=policy_all.kinds[JobKind.SMTP_TEST_SEND].lease_s
        ),
        erasure_alert_after_s=settings.erasure_alert_after_s,
    )

    async def smtp_probe() -> None:
        # one whole probe; the tick's own timeout (slots.probe_tick_timeout_s) is longer
        await run_probe(sender, engine.connect, timeout_s=sender.worst_case_s())

    handlers: dict[Any, Any] = {JobKind.TRANSCRIPT_DELIVER: make_deliver_handler(deps)}
    # the notification emails (REQ-071, REQ-086): the same relay, recipients and schedule
    # ... and the operator's test send (plan 12b, review #5): one send through the same relay
    for kind in (
        JobKind.TEACHER_NOTIFY,
        JobKind.OPERATOR_NOTIFY,
        JobKind.SMTP_TEST_SEND,
    ):
        kp = policy_all.kinds[kind]
        if kp.budget_s < need:
            raise StartupValidationError(
                step=STEP,
                detail=(
                    f"{kind.value} budget {kp.budget_s:g} s is shorter than one delivery's "
                    f"worst case {need:g} s"
                ),
                remediation=(
                    f"Raise VEKTRA_ASSESS_JOB_{kind.value.upper()}_BUDGET_S (the lease "
                    "follows at 2x) or lower VEKTRA_ASSESS_SMTP_TIMEOUT_S."
                ),
            )
    notify_deps = NotifyDeps(
        recipients=deps.recipients,
        sender=sender,
        sender_address=deps.sender_address,
        message_id_domain=deps.message_id_domain,
        domains=domains,
        operator_address=settings.operator_email,
        schedule=deps.schedule,
        window=deps.window,
        # the crash window: no run of either kind outlives its lease
        stale_after=timedelta(
            seconds=max(
                policy_all.kinds[k].lease_s
                for k in (JobKind.TEACHER_NOTIFY, JobKind.OPERATOR_NOTIFY)
            )
        ),
        db_budget=row_budget,
    )
    handlers[JobKind.TEACHER_NOTIFY] = make_notify_handler(
        notify_deps, JobKind.TEACHER_NOTIFY
    )
    if settings.operator_email:
        # without the address the slot stays unbound (refused at startup outside development)
        handlers[JobKind.OPERATOR_NOTIFY] = make_notify_handler(
            notify_deps, JobKind.OPERATOR_NOTIFY
        )
        from vektra_assess.delivery.smtp_test import (
            SmtpTestDeps,
            make_smtp_test_handler,
        )

        # plan 12b: the operator's test send, to the same fixed address, through the same sender
        handlers[JobKind.SMTP_TEST_SEND] = make_smtp_test_handler(
            SmtpTestDeps(
                sender=sender,
                sender_address=deps.sender_address,
                message_id_domain=deps.message_id_domain,
                operator_address=settings.operator_email,
            )
        )
    return DeliveryParts(
        handlers,
        {"delivery_retry": delivery_retry, "smtp_probe": smtp_probe},
    )


def _privacy_parts(checked: Checked) -> tuple[dict[Any, Any], dict[str, Any]]:
    """The retention purge (daily tick, and the ``purge`` job: the same run on demand) and the
    ``erasure`` job (plan assess-privacy-retention). Both run on the worker's own engine: the
    API never deletes. Without a database backup retention the operator is warned that
    crypto-shredding does not reach backups that still hold wrapped keys (ARCH-079)."""
    from vektra_assess.jobs import JobKind
    from vektra_assess.privacy.erasure import make_erasure_handler
    from vektra_assess.privacy.purge import make_purge_handler, run_purge

    settings, engine = checked.settings, checked.engine
    if settings.db_backup_retention_days is None:
        log.warning(
            "assess_db_backup_retention_unset",
            reason_code="crypto_shred_scope_unknown",
            remediation=(
                "Set VEKTRA_ASSESS_DB_BACKUP_RETENTION_DAYS: a purge removes the keys from the "
                "database, not from backups taken before it"
            ),
        )

    async def purge() -> None:
        await run_purge(engine.connect)

    handlers: dict[Any, Any] = {
        JobKind.PURGE: make_purge_handler(engine.connect),
        JobKind.ERASURE: make_erasure_handler(engine.connect),
    }
    return handlers, {"purge": purge}


@dataclass(frozen=True, slots=True)
class SetupCheckParts:
    handlers: Mapping[Any, Any]
    ticks: Mapping[str, Any]
    registry: Any | None = None  # the item registry the handler runs (None: not bound)


def _setup_check_parts(
    checked: Checked, smtp_probe: Any | None = None
) -> SetupCheckParts:
    """The REQ-079 ``setup_check`` handler and its daily tick (the web-service plan, task F4, carried
    over to plan assess-job-loop). Bound only from a key set that passed the key matrix and
    holds every secret the worker's LMS provider declares (``required_secrets``); otherwise
    both stay unbound and ``assert_all_bound`` refuses to start.

    The item registry is built per composition (never the module-level default, which a second
    composition would register into twice): the built-ins plus the parser's
    ``parser_sidecar_running`` contributor (plan assess-parser, "aggregated via the plan 3
    setup-check slot"), and the worker's own ``smtp_probe`` and ``db_log_statement_none`` items
    (``worker/setup_items.py``, coordinator decisions 2026-11-08).
    """
    lms = _bound_lms(checked)
    if lms is None:
        return SetupCheckParts({}, {})
    from vektra_assess.jobs import JobKind
    from vektra_assess.worker.parser_client import ParserClient, register_parser_check
    from vektra_assess.worker.policy import load_policy
    from vektra_assess.worker.setup_check import (
        make_setup_check_handler,
        queue_setup_checks,
    )
    from vektra_assess.worker.setup_items import fresh_registry, register_worker_items

    settings, engine = checked.settings, checked.engine
    registry = fresh_registry()
    register_parser_check(ParserClient.from_settings(settings), registry)
    register_worker_items(
        registry,
        engine.connect,
        smtp_configured=bool(settings.smtp_host and settings.smtp_from),
        # the item probes itself when the tick's row is missing or stale (finding 1)
        smtp_probe=smtp_probe,
    )

    async def setup_check() -> None:
        """The daily tick body: queue one setup check per enabled site."""
        await queue_setup_checks(engine.connect)

    return SetupCheckParts(
        {
            JobKind.SETUP_CHECK: make_setup_check_handler(
                engine,
                lms,
                settings,
                registry=registry,
                # a running report older than the job's lease is a dead run's (finding 6)
                orphan_after_s=load_policy(
                    os.environ, lms_call_deadline_s=_lms_call_deadline(checked)
                )
                .kinds[JobKind.SETUP_CHECK]
                .lease_s,
            )
        },
        {"setup_check": setup_check},
        registry,
    )


def make_outage_probe(settings: Any) -> Any:
    """The outage probe over the discussion provider, built as every discussion call is
    (``build_assess_llm``: the model, the key, ``VEKTRA_ASSESS_LLM_EXTRA_BODY``) and called as a
    turn is: the same max_tokens, temperature, seed and first-token deadline
    (``VEKTRA_ASSESS_OUTAGE_TIMEOUT_S``)."""
    from vektra_app.assess_llm import build_assess_llm
    from vektra_assess.worker.outage_probe import make_llm_probe

    return make_llm_probe(
        build_assess_llm(settings),
        str(settings.llm_model),
        timeout_s=float(settings.outage_timeout_s),
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        seed=settings.llm_seed,
    )


def _session_ticks(checked: Checked) -> dict[str, Any]:
    """The plan assess-session-lifecycle tick bodies (they need no key)."""
    from vektra_assess.worker.enrolment import queue_rechecks
    from vektra_assess.worker.outage_probe import outage_tick
    from vektra_assess.worker.reaper import reap_starting
    from vektra_assess.worker.render import gate_refresh_tick
    from vektra_assess.worker.reply_reconcile import queue_reconciles
    from vektra_assess.worker.start_guard import StartLimits
    from vektra_assess.worker.sweeper import sweep_timed_out

    settings, engine = checked.settings, checked.engine

    async def starting_reaper() -> None:
        await reap_starting(
            checked.liveness_engine.connect,  # its own engine: never behind job work
            settings.start_t_s,
            limits=StartLimits.from_settings(settings),
        )

    async def gate_refresh() -> None:
        await gate_refresh_tick(engine.connect, settings.render_fresh_s)

    async def sweeper() -> None:
        await sweep_timed_out(engine.connect)
        await queue_reconciles(engine.connect, settings.outage_timeout_s)

    async def enrolment_recheck() -> None:
        await queue_rechecks(engine.connect)

    # one discussion provider per deployment (OQ-032): one probe for every OUTAGE session. Built
    # lazily on the first OUTAGE (nothing connects at composition); without a configured model
    # the tick still closes the sessions whose outage passed C
    probe_box: list[Any] = []

    async def probe() -> bool:
        if not probe_box:
            probe_box.append(make_outage_probe(settings))
        return bool(await probe_box[0]())

    async def outage_probe() -> None:
        await outage_tick(
            engine.connect,
            probe if settings.llm_model else None,
            min_credit_s=settings.outage_g_s,
        )

    return {
        "outage_probe": outage_probe,
        "starting_reaper": starting_reaper,
        "gate_refresh": gate_refresh,
        "sweeper": sweeper,
        "enrolment_recheck": enrolment_recheck,
    }


def _compose(checked: Checked, bindings: Any | None = None) -> Any:
    """Build runtime, scheduler, heartbeat and control; nothing is started or connected yet."""
    from vektra_assess.jobs import JobKind
    from vektra_assess.worker.audit import make_audit
    from vektra_assess.worker.cleanup import cleanup_done_jobs, cleanup_expired_jtis
    from vektra_assess.worker.heartbeat import Heartbeat
    from vektra_assess.worker.listener import ControlConnection
    from vektra_assess.worker.policy import WorkerPolicyError, load_policy
    from vektra_assess.worker.reconcile import reap_exhausted
    from vektra_assess.worker.runtime import SqlStore, WorkerRuntime
    from vektra_assess.worker.slots import (
        Bindings,
        SlotsNotBound,
        assert_all_bound,
        build_slots,
    )
    from vektra_assess.worker.ticks import TickScheduler

    settings, engine = checked.settings, checked.engine
    try:
        policy = load_policy(
            os.environ, lms_call_deadline_s=_lms_call_deadline(checked)
        )
    except WorkerPolicyError as exc:
        raise StartupValidationError(
            step=STEP,
            detail=str(exc),
            remediation="Fix the VEKTRA_ASSESS_JOB_* / VEKTRA_ASSESS_POOL_* settings.",
        ) from exc

    @asynccontextmanager
    async def acquire_raw() -> AsyncIterator[Any]:
        """An asyncpg connection from the SQLAlchemy pool: one pool, one budget."""
        async with engine.connect() as sa_conn:
            raw = await sa_conn.get_raw_connection()
            yield raw.driver_connection

    liveness = checked.liveness_engine

    @asynccontextmanager
    async def acquire_liveness() -> AsyncIterator[Any]:
        """The heartbeat's and the lease reaper's connection: their own small engine, so a job
        pool exhausted by slow handlers never makes a beat wait (plan 13a review)."""
        async with liveness.connect() as sa_conn:
            raw = await sa_conn.get_raw_connection()
            yield raw.driver_connection

    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    audit = make_audit(engine.connect)
    runtime = WorkerRuntime(
        SqlStore(acquire_raw),
        worker_id=worker_id,
        pools=policy.pools,
        acquire_db=engine.connect,
        audit=audit,
        poll_interval_s=settings.job_poll_interval_s,
        backoff_base_s=settings.job_backoff_base_s,
        backoff_cap_s=settings.job_backoff_cap_s,
    )
    wiring: dict[
        str, Any
    ] = {}  # the scheduler needs the heartbeat, the heartbeat its stats

    async def on_lost() -> None:
        await wiring["scheduler"].cancel_running()

    control = ControlConnection(
        _asyncpg_dsn(checked.worker_url), on_notify=runtime.wake, on_lost=on_lost
    )
    from vektra_assess.worker.parser_health import ParserHealth

    # plan 12b: the parser status /health/assess reports, from the parser_ping tick
    parser_health = ParserHealth()
    heartbeat = Heartbeat(
        acquire_liveness,
        worker_id=worker_id,
        version=_version(),
        file_path=settings.worker_heartbeat_file,
        stats=lambda: {
            **runtime.stats(),
            **wiring["scheduler"].stats(),
            **parser_health.stats(),
        },
    )

    async def cleanup() -> None:
        await cleanup_done_jobs(acquire_raw, settings.job_done_retention_h)
        await cleanup_expired_jtis(acquire_raw)
        # plan 12b: setup-check reports whose job died would read "queued" forever
        from vektra_assess.worker.setup_check import fail_abandoned_reports

        await fail_abandoned_reports(checked.engine.connect)

    # the reaper's "dead" audit rows go through the liveness engine too
    liveness_audit = make_audit(liveness.connect)

    async def lease_reaper() -> None:
        await reap_exhausted(acquire_liveness, liveness_audit)

    # ``bindings`` is the test seam (chaos harness): it replaces the production bodies entirely
    delivery = _delivery_parts(checked)
    # one LMS provider for every caller (``checked.lms``): one rate cap, one breaker registry,
    # one pool
    setup = _setup_check_parts(checked, delivery.ticks.get("smtp_probe"))
    privacy_handlers, privacy_ticks = _privacy_parts(checked)
    from vektra_assess.worker.parser_client import ParserClient

    parser_for_health = ParserClient.from_settings(settings)

    async def parser_ping() -> None:
        await parser_health.check(parser_for_health)

    privacy_ticks = {**privacy_ticks, "parser_ping": parser_ping}
    extra = bindings or Bindings(
        handlers={
            **_session_handlers(
                checked, audit, policy.kinds[JobKind.PREFLIGHT].budget_s
            ),
            **delivery.handlers,
            **setup.handlers,
            **privacy_handlers,
        },
        ticks={
            **_session_ticks(checked),
            **delivery.ticks,
            **setup.ticks,
            **privacy_ticks,
        },
    )
    slots = build_slots(
        policy,
        settings,
        Bindings(
            handlers=extra.handlers,
            ticks={
                "heartbeat": heartbeat.beat,
                "job_cleanup": cleanup,
                "lease_reaper": lease_reaper,
                **extra.ticks,
            },
        ),
    )
    try:
        assert_all_bound(slots, enabled=not settings.worker_allow_unbound)
    except SlotsNotBound as exc:
        raise StartupValidationError(
            step=STEP,
            detail=str(exc),
            remediation=(
                "Bind every handler (later plans of the assess feature), or for development "
                "only set VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND=true with VEKTRA_ENV=development."
            ),
        ) from exc
    if settings.worker_allow_unbound and slots.unbound():
        log.warning("assess_worker_unbound_slots", slots=slots.unbound())
    for h in slots.handlers:
        runtime.register_handler(h)
    scheduler = TickScheduler(control, list(slots.ticks))
    wiring["scheduler"] = scheduler
    return runtime, scheduler, heartbeat, control, acquire_raw, audit


async def _serve(
    checked: Checked, stop: asyncio.Event, bindings: Any | None = None
) -> None:
    from vektra_assess.worker.listener import ControlConnectionError
    from vektra_assess.worker.reconcile import ReconcileError, reconcile
    from vektra_assess.worker.shutdown import shutdown
    from vektra_assess.worker.watchdog import watchdog

    runtime, scheduler, heartbeat, control, acquire_raw, audit = _compose(
        checked, bindings
    )
    # One deadline for everything before "ready": connecting, the NOTIFY self-test, reconciliation
    # and the first heartbeat. The watchdog only judges a worker that is already ready, so without
    # this a statement stuck on a half-open connection during startup would hang the process.
    startup_s = checked.settings.worker_wedge_exit_s
    startup_deadline = asyncio.timeout(startup_s)
    supervisor: asyncio.Task[None] | None = None
    wedge: asyncio.Task[None] | None = None
    startup_ticks: asyncio.Task[None] | None = None
    try:
        try:
            async with startup_deadline:
                try:
                    await control.start()  # refuses behind a transaction-mode pooler
                except ControlConnectionError as exc:
                    raise StartupValidationError(
                        step=STEP,
                        detail=str(exc),
                        remediation="Give the worker a direct or session-mode Postgres connection.",
                    ) from exc
                supervisor = asyncio.create_task(control.supervise())
                try:
                    # Startup ticks are NOT run inside the reconcile lock: a slow one (the setup
                    # check makes about twenty live LMS calls) would hold the lock, delay the
                    # first heartbeat and the claim loop, trip the container healthcheck and
                    # ignore SIGTERM.
                    await reconcile(control, acquire_raw, audit)
                except ReconcileError as exc:
                    raise StartupValidationError(
                        step=STEP,
                        detail=str(exc),
                        remediation="Stop the other worker or wait for its reconciliation to finish.",
                    ) from exc
                await heartbeat.beat()
                heartbeat.set_state("ready")
                await heartbeat.beat()
        except TimeoutError:
            if not startup_deadline.expired():
                raise  # an inner timeout (connect, statement): keep its own cause
            raise StartupValidationError(
                step=STEP,
                detail=f"startup did not complete within {startup_s:g} s",
                remediation=(
                    "The database is unreachable or not answering. Check the worker DSN and the "
                    "network; the supervisor restarts the worker."
                ),
            ) from None
        scheduler.start()
        startup_ticks = asyncio.create_task(scheduler.run_startup_ticks())
        run_task = asyncio.create_task(runtime.run())
        wedge = asyncio.create_task(
            watchdog(heartbeat, stop, exit_after_s=checked.settings.worker_wedge_exit_s)
        )
        log.info("assess_worker_started", role=WORKER_ROLE, worker_id=runtime.worker_id)
        await stop.wait()
        released = await shutdown(
            runtime=runtime,
            run_task=run_task,
            scheduler=scheduler,
            heartbeat=heartbeat,
            control=control,
            drain_s=checked.settings.worker_drain_s,
        )
        log.info("assess_worker_stopped", released=released)
    finally:
        for background in (wedge, startup_ticks):
            if background is not None:
                background.cancel()
        if supervisor is not None:
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await supervisor
        with contextlib.suppress(Exception):
            async with asyncio.timeout(3):
                await control.stop()


def check_imports() -> None:
    """``--check-imports``: import the whole composition and assert the ceiling, no DB or keys."""
    import importlib

    importlib.import_module(
        "vektra_core.providers.litellm_provider"
    )  # the LLM provider's cost
    for name in ("handler", "probe", "smtp"):  # plan transcript-delivery 10b
        importlib.import_module(f"vektra_assess.delivery.{name}")
    for name in (
        "audit", "cleanup", "heartbeat", "listener", "policy", "preflight", "reconcile",
        "enrolment", "outage_probe", "reaper", "render", "reply_reconcile", "runtime", "setup_check", "setup_items", "shutdown", "slots", "start_guard", "start_source", "transcript_gen",
        "sweeper", "ticks",
        "watchdog",
    ):  # fmt: skip
        importlib.import_module(f"vektra_assess.worker.{name}")
    assert_import_ceiling()
    print("ceiling ok")


async def _amain(
    bindings: Any | None = None, lms_entry_points: Any | None = None
) -> int:
    # A heartbeat left by a previous run must not vouch for this one. The settings may be
    # invalid here: then the path comes from the variable or the default, and the guarded
    # ``_checks`` below reports the error by name, never with the rejected value
    from pydantic import ValidationError

    from vektra_assess.config import VektraAssessSettings
    from vektra_assess.worker.shutdown import install_signal_handlers

    try:
        heartbeat_file = VektraAssessSettings().worker_heartbeat_file
    except ValidationError:
        field = VektraAssessSettings.model_fields["worker_heartbeat_file"]
        from pathlib import Path

        heartbeat_file = Path(os.environ.get(str(field.alias), str(field.default)))
    with contextlib.suppress(FileNotFoundError):
        heartbeat_file.unlink()
    try:
        checked = await _checks(lms_entry_points)
    except DisabledError as exc:
        print(f"[ASSESS WORKER] {exc}", file=sys.stderr)
        return 2
    except StartupValidationError as exc:
        print(exc.to_plain_text(), file=sys.stderr)
        return 1

    stop = asyncio.Event()
    remove_handlers = install_signal_handlers(stop)
    try:
        await _serve(checked, stop, bindings)
    except StartupValidationError as exc:
        print(exc.to_plain_text(), file=sys.stderr)
        return 1
    finally:
        remove_handlers()
        # Bounded: a pooled connection with a pending query cancel can make ``dispose`` wait on a
        # silent peer without limit, and the process would then outlive its own shutdown.
        with contextlib.suppress(Exception):
            async with asyncio.timeout(5):
                await checked.engine.dispose()
                if checked.liveness is not None:
                    await checked.liveness.dispose()
        if checked.lms is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(5):
                    await checked.lms.aclose()
    return 0


def main(bindings: Any | None = None, lms_entry_points: Any | None = None) -> None:
    """Entry point. ``bindings`` and ``lms_entry_points`` are test seams: the chaos harness
    binds its own bodies to production slots and offers its development-only provider
    (a test double, loaded only with ``VEKTRA_ENV=development``);
    ``python -m vektra_app.assess_worker`` always runs with ``None``."""
    if "--check-imports" in sys.argv[1:]:
        check_imports()
        raise SystemExit(0)
    # JSON on stderr with the assess allowlist on structlog AND stdlib records (plan
    # assess-context A): before this the worker's stdlib INFO lines went nowhere.
    from vektra_assess import LOGGER_PREFIX
    from vektra_shared.log_safety import (
        configure_assess_worker_logging,
        register_strict_logger_prefix,
    )

    register_strict_logger_prefix(LOGGER_PREFIX)
    configure_assess_worker_logging()
    raise SystemExit(asyncio.run(_amain(bindings, lms_entry_points)))


if __name__ == "__main__":
    main()

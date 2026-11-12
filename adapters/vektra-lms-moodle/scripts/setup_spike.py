"""Operator CLI: run the full setup check and the capability probes against a dev site.

    uv run --group lms-moodle python adapters/vektra-lms-moodle/scripts/setup_spike.py \\
        --site http://localhost:8080 --manifest <seed manifest.json> \\
        --token-file <provision.json> --allow-http --allow-cidr 127.0.0.0/8 --resolve-ipv4

No job loop is needed. It prints the item table and the capability matrix and nothing else:
flag names, item names, results, reason codes, a short evidence hash prefix and the release.
Never a token, a host, an id or any Moodle content.

- ``--no-db`` (default when no DSN is given): only prints.
- with a DSN (``--db-url`` or ``VEKTRA_ASSESS_WORKER_DATABASE_URL``, the WORKER role): the site
  must be registered in ``assess_sites`` (matched by ``--site``); the probe manifest derived
  from ``--manifest`` is stored in ``assess_sites.probe_manifest`` and the run goes through
  ``run_setup_check``, the same code the worker handler calls (one transaction, a report row).
- the token comes from ``--token-file`` (a ``provision-*.json`` with ``service_account.token``,
  or a file holding just the token) or, with a database, from the worker's ``assess_ws_tokens``
  secret like the worker does.

Exit status: 0 when the run completed and no flag is ``not_probed``; 1 otherwise; 2 on a
usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_assess.config import VektraAssessSettings
from vektra_assess.setup import CheckOutcome, evaluate, run_setup_check
from vektra_lms_moodle.client import MoodleWsClient
from vektra_lms_moodle.config import MoodleSettings
from vektra_lms_moodle.probes import derive_probe_manifest
from vektra_lms_moodle.provider import MoodleLmsProvider
from vektra_lms_moodle.tokens import secret_token_provider
from vektra_shared.lms import LmsSite


class UsageError(Exception):
    pass


def ipv4_resolver(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
    """``localhost`` resolves to ::1 first, where the dev containers do not listen."""
    found = socket.getaddrinfo(host, port, *args, **kwargs)
    return [r for r in found if r[0] == socket.AF_INET] or list(found)


def read_token(path: Path, key: str) -> str:
    raw = path.read_text().strip()
    try:
        doc: Any = json.loads(raw)
    except ValueError:
        doc = raw
    if isinstance(doc, dict):
        for part in key.split("."):
            doc = doc.get(part) if isinstance(doc, dict) else None
    if not isinstance(doc, str) or not doc:
        raise UsageError("no token found in the token file (see --token-key)")
    return doc


def build_settings(
    args: argparse.Namespace,
) -> tuple[VektraAssessSettings, MoodleSettings]:
    """The vertical's settings (the generic items, the worker DSN) and the provider's own."""
    base = VektraAssessSettings()
    update: dict[str, Any] = {}
    for arg, field in (("ui_origin", "ui_origin"), ("api_origin", "api_public_origin")):
        if getattr(args, arg):
            update[field] = getattr(args, arg)
    moodle = MoodleSettings()
    m_update: dict[str, Any] = {}
    if args.allow_http:
        m_update["moodle_allow_http"] = True
    if args.allow_cidr:
        m_update["moodle_allowed_cidrs"] = ",".join(args.allow_cidr)
    if args.ingest_account:
        m_update["ingest_account_username"] = args.ingest_account
    return base.model_copy(update=update), moodle.model_copy(update=m_update)


def render_table(outcome: CheckOutcome) -> str:
    lines = [f"release: {outcome.release or 'unknown'}", "", "ITEMS"]
    width = max(len(i.name) for i in outcome.items)
    for i in outcome.items:
        lines.append(f"  {i.name:<{width}}  {i.result:<12} {i.reason_code or '-'}")
    lines += ["", "CAPABILITY MATRIX"]
    if not outcome.flags:
        lines.append("  (not probed: the site did not answer site_info)")
    else:
        w = max(len(f.flag) for f in outcome.flags)
        for f in outcome.flags:
            lines.append(
                f"  {f.flag:<{w}}  {f.state:<10} {f.reason_code or '-':<26} {f.evidence_hash[:12]}"
            )
    return "\n".join(lines)


def render_json(outcome: CheckOutcome) -> str:
    return json.dumps(
        {
            "release": outcome.release,
            "items": [i.as_json() for i in outcome.items],
            "capabilities": [
                {
                    "flag": f.flag,
                    "state": f.state,
                    "reason_code": f.reason_code,
                    "evidence_hash": f.evidence_hash,
                }
                for f in outcome.flags
            ],
        },
        indent=2,
    )


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="setup_spike.py", description=__doc__)
    ap.add_argument(
        "--site", required=True, help="base URL of the Moodle site (never printed)"
    )
    ap.add_argument("--manifest", type=Path, help="seed manifest JSON of the dev site")
    ap.add_argument("--token-file", type=Path)
    ap.add_argument("--token-key", default="service_account.token")
    ap.add_argument(
        "--json", action="store_true", help="print JSON instead of the tables"
    )
    ap.add_argument("--no-db", action="store_true", help="never touch a database")
    ap.add_argument("--db-url", help="DSN of the vektra_assess_worker role")
    ap.add_argument(
        "--resolve-ipv4", action="store_true", help="resolve the host to IPv4 only"
    )
    ap.add_argument(
        "--allow-http",
        action="store_true",
        help="plain http (VEKTRA_ENV=development only)",
    )
    ap.add_argument("--allow-cidr", action="append", default=[], metavar="CIDR")
    ap.add_argument(
        "--ingest-account", help="ingestion account username (overrides the setting)"
    )
    ap.add_argument("--ui-origin", help="chat UI origin (overrides the setting)")
    ap.add_argument("--api-origin", help="API public origin (overrides the setting)")
    return ap


async def _run(args: argparse.Namespace) -> CheckOutcome:
    settings, moodle = build_settings(args)
    seed: dict[str, Any] | None = (
        json.loads(args.manifest.read_text()) if args.manifest else None
    )
    probe_manifest = derive_probe_manifest(seed) if seed else None
    dsn = None if args.no_db else (args.db_url or settings.worker_database_url)
    resolver: Callable[..., Any] = (
        ipv4_resolver if args.resolve_ipv4 else socket.getaddrinfo
    )
    base_url = args.site.rstrip("/")

    if dsn is None:
        if args.token_file is None:
            raise UsageError("--token-file is required without a database")
        token = read_token(args.token_file, args.token_key)
        client = MoodleWsClient(
            moodle, token_provider=lambda _s: token, resolver=resolver
        )
        try:
            return await evaluate(
                MoodleLmsProvider(client, moodle),
                LmsSite("spike", base_url),
                settings,
                probe_manifest,
            )
        finally:
            await client.aclose()

    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text("SELECT id FROM assess_sites WHERE rtrim(base_url, '/') = :u"),
                    {"u": base_url},
                )
            ).one_or_none()
            if row is None:
                raise UsageError("the site is not registered in assess_sites")
            site_id = row[0]
            if probe_manifest is not None:
                await conn.execute(
                    text(
                        "UPDATE assess_sites SET probe_manifest = CAST(:m AS jsonb) WHERE id = :s"
                    ),
                    {"m": json.dumps(probe_manifest), "s": site_id},
                )
        if args.token_file is not None:
            token = read_token(args.token_file, args.token_key)
            provider: Callable[[str], str | None] = lambda _s: token  # noqa: E731
        else:
            provider = secret_token_provider(moodle.secrets_dir)
        client = MoodleWsClient(moodle, token_provider=provider, resolver=resolver)
        try:
            return await run_setup_check(
                engine,
                site_id=site_id,
                provider=MoodleLmsProvider(client, moodle),
                settings=settings,
            )
        finally:
            await client.aclose()
    finally:
        await engine.dispose()


def quiet_logging() -> None:
    """stdout carries only the report: operational events go to stderr, warnings and above."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    quiet_logging()
    try:
        outcome = asyncio.run(_run(args))
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # a missing or malformed file: the class only, a path or a value could be a secret
        print(
            f"error: cannot use the input files ({type(exc).__name__})", file=sys.stderr
        )
        return 2
    print(render_json(outcome) if args.json else render_table(outcome))
    return (
        1
        if any(f.reason_code == "not_probed" for f in outcome.flags)
        or not outcome.flags
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())

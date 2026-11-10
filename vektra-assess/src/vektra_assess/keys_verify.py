"""``python -m vektra_assess.keys verify``: do the loaded keys open the data in this database?

Plan assess-privacy-retention 12b. It catches a restore with the wrong key set: the database came
back, the secrets did not (or came back from another deployment), and nothing fails until a
transcript or a session is opened. Run it in the worker container (``--role worker``, the default)
and in the API container (``--role api``) after a restore and after a key change.

For every key purpose the role holds, the check reads the key ids the database references
(``DISTINCT`` over the rows that still carry ciphertext) and:

- a referenced kid that is not in the loaded keyring is a MISMATCH (``kid_not_loaded``);
- for one row per kid it tries to open the ciphertext with the loaded key. A kid that is present
  but does not open its row is a MISMATCH (``wrong_key``): same name, different key bytes;
- a purpose the role cannot read (no privilege) is reported ``skipped``, never as a pass.

It prints, per keyring, the kids and a short fingerprint (``sha256(key)[:12]``) so two
deployments can be compared by eye, and nothing else: no key byte, no plaintext, no row content.
Exit 0 when nothing mismatches, 1 on any mismatch, 2 when it cannot run (settings, secrets,
database).

Key purposes with no database reference are listed as such: ``assess_token_signing`` (tokens
live minutes and are never stored) and the subject reference, which is an unkeyed
``sha256(site|userid)``: there is no subject-key secret to lose.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TextIO

from vektra_assess.keys import AssessKeyring, Loaded

OK, MISMATCH, SKIPPED = "ok", "MISMATCH", "skipped"


def fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:12]


@dataclass(frozen=True)
class Check:
    """One key purpose: which keyring, which kids the DB references, how to open one row."""

    keyring: str
    label: str
    kids_sql: str
    #: SQL returning one sample row for a kid (``:kid``), or None: presence only
    sample_sql: str | None = None
    #: opens the sample row with the keyring; raises on a wrong key
    opener: Callable[[AssessKeyring, Any], object] | None = None


@dataclass
class Finding:
    check: str
    status: str
    kid: str | None = None
    reason: str | None = None


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    fingerprints: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def mismatched(self) -> bool:
        return any(f.status == MISMATCH for f in self.findings)


def _open_kek(ring: AssessKeyring, row: Any) -> object:
    from vektra_assess.crypto.kek import unwrap_dek

    return unwrap_dek(ring, bytes(row[1]), row[0], int(row[2]))


def _open_transcript(ring: AssessKeyring, row: Any) -> object:
    from vektra_assess.transcript.store import open_verified

    return open_verified(ring, row[0], bytes(row[1]), bytes(row[2]), row[3])


def _open_reason(table: str) -> Callable[[AssessKeyring, Any], object]:
    def opener(ring: AssessKeyring, row: Any) -> object:
        from vektra_assess.crypto.teacher_reason import decrypt_reason

        return decrypt_reason(ring, table, row[0], bytes(row[1]))  # type: ignore[arg-type]

    return opener


def _reason_check(table: str) -> Check:
    return Check(
        "assess_transcript_kek",
        f"{table}.reason_kid",
        f"SELECT DISTINCT reason_kid FROM {table} WHERE reason_kid <> 'purged'",
        f"SELECT id, reason_ct FROM {table} WHERE reason_kid = :kid LIMIT 1",
        _open_reason(table),
    )


CHECKS: tuple[Check, ...] = (
    Check(
        "assess_kek",
        "assess_session_keys.kek_kid",
        "SELECT DISTINCT kek_kid FROM assess_session_keys WHERE kek_wrapped IS NOT NULL",
        "SELECT session_id, kek_wrapped, key_version FROM assess_session_keys "
        "WHERE kek_kid = :kid AND kek_wrapped IS NOT NULL LIMIT 1",
        _open_kek,
    ),
    Check(
        "assess_transcript_kek",
        "assess_transcripts.transcript_kid",
        "SELECT DISTINCT transcript_kid FROM assess_transcripts WHERE dek_wrapped IS NOT NULL",
        "SELECT id, ciphertext, dek_wrapped, content_hash FROM assess_transcripts "
        "WHERE transcript_kid = :kid AND dek_wrapped IS NOT NULL "
        "AND ciphertext IS NOT NULL LIMIT 1",
        _open_transcript,
    ),
    _reason_check("assess_reset_grants"),
    _reason_check("assess_terminations"),
    Check(
        "assess_teacher_config_private",
        "assess_activity_config_sealed.kid",
        "SELECT DISTINCT kid FROM assess_activity_config_sealed WHERE sealed_blob IS NOT NULL",
    ),
    # in practice fingerprint-only: the API, which holds the private key, may not read the
    # hand-off table (it reads a live hand-off through a definer function), so this reports
    # ``skipped``; the worker reads the table but never holds the private key (CodeRabbit
    # 4525094929). Kept so a future grant makes it a real presence check.
    Check(
        "assess_handoff_private",
        "assess_session_handoff.handoff_kid",
        "SELECT DISTINCT handoff_kid FROM assess_session_handoff",
    ),
)

NOT_REFERENCED = {
    "assess_token_signing": "tokens are never stored",
}


Query = Callable[[str, Mapping[str, Any]], Awaitable[Sequence[Any] | None]]


async def verify(loaded: Mapping[str, Loaded], query: Query) -> Report:
    """``query(sql, params)`` returns rows, or ``None`` when this role may not read them."""
    report = Report()
    for name, value in sorted(loaded.items()):
        if isinstance(value, AssessKeyring):
            report.fingerprints[name] = {
                kid: fingerprint(key) for kid, key in sorted(value.keys.items())
            }
    for check in CHECKS:
        ring = loaded.get(check.keyring)
        if not isinstance(ring, AssessKeyring):
            continue  # this role does not hold the key: the other container checks it
        rows = await query(check.kids_sql, {})
        if rows is None:
            report.findings.append(Finding(check.label, SKIPPED, reason="not_readable"))
            continue
        kids = sorted({str(r[0]) for r in rows if r[0] is not None})
        if not kids:
            report.findings.append(Finding(check.label, OK, reason="no_rows"))
        for kid in kids:
            if kid not in ring.keys:
                report.findings.append(
                    Finding(check.label, MISMATCH, kid, "kid_not_loaded")
                )
                continue
            if check.sample_sql is None or check.opener is None:
                report.findings.append(Finding(check.label, OK, kid, "kid_present"))
                continue
            sample = await query(check.sample_sql, {"kid": kid})
            if not sample:
                report.findings.append(Finding(check.label, OK, kid, "kid_present"))
                continue
            try:
                check.opener(ring, sample[0])
            except Exception as exc:
                report.findings.append(
                    Finding(
                        check.label, MISMATCH, kid, f"wrong_key:{type(exc).__name__}"
                    )
                )
            else:
                report.findings.append(Finding(check.label, OK, kid, "opens"))
    for name, why in NOT_REFERENCED.items():
        if name in loaded:
            report.findings.append(Finding(name, OK, reason=f"not_referenced ({why})"))
    return report


def render(report: Report, out: TextIO) -> None:
    for name, kids in report.fingerprints.items():
        shown = ", ".join(f"{k}={fp}" for k, fp in kids.items())
        print(f"key {name}: {shown}", file=out)
    for f in report.findings:
        kid = f" kid={f.kid}" if f.kid else ""
        reason = f" ({f.reason})" if f.reason else ""
        print(f"{f.status} {f.check}{kid}{reason}", file=out)
    print("RESULT: " + ("MISMATCH" if report.mismatched else "ok"), file=out)


def make_query(engine: Any) -> Query:
    """The ``query`` callable over ``engine``: rows, or ``None`` on a permission denial (the
    role may not read that table), any other database error raised."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    async def query(sql: str, params: Mapping[str, Any]) -> Sequence[Any] | None:
        try:
            async with engine.connect() as conn:
                return list((await conn.execute(text(sql), dict(params))).all())
        except DBAPIError as exc:
            if "permission denied" in str(exc.orig).lower():
                return None
            raise

    return query


async def _run(role: Literal["worker", "api"], out: TextIO) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    from vektra_assess.config import VektraAssessSettings, explain_settings_error
    from vektra_assess.db.roles import dsn_with_password
    from vektra_assess.keys import enforce_key_matrix

    try:
        settings = VektraAssessSettings()
    except Exception as exc:
        detail, _ = explain_settings_error(exc)
        print(f"cannot run: {detail}", file=out)
        return 2
    try:
        loaded = enforce_key_matrix(
            role, secrets_dir=settings.secrets_dir, step="keys_verify"
        )
    except Exception as exc:
        print(
            f"cannot run: {type(exc).__name__}: {getattr(exc, 'detail', '')}", file=out
        )
        return 2
    url = (
        settings.worker_database_url if role == "worker" else settings.api_database_url
    )
    secret = (
        "assess_db_worker_password" if role == "worker" else "assess_db_api_password"
    )
    if not url or secret not in loaded:
        print(
            "cannot run: the role's database URL or password secret is missing",
            file=out,
        )
        return 2
    try:
        # a DSN that carries a password, or one the driver refuses, means the check cannot run
        # (exit 2, never 1, which means a key mismatch); the exception text may hold the DSN,
        # so only its type is printed (CodeRabbit 4524508958)
        engine = create_async_engine(
            dsn_with_password(url, loaded[secret].value, step="keys_verify")  # type: ignore[union-attr]
        )
    except Exception as exc:
        print(f"cannot run: database URL {type(exc).__name__}", file=out)
        return 2
    query = make_query(engine)

    try:
        report = await verify(loaded, query)
    except Exception as exc:
        print(f"cannot run: database {type(exc).__name__}", file=out)
        return 2
    finally:
        await engine.dispose()
    render(report, out)
    return 1 if report.mismatched else 0


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m vektra_assess.keys verify")
    parser.add_argument("--role", choices=("worker", "api"), default="worker")
    args = parser.parse_args(list(argv))
    return asyncio.run(_run(args.role, sys.stdout))

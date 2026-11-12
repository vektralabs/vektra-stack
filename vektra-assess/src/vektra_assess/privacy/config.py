"""REQ-089 privacy settings of a site (plan assess-privacy-retention, revision 0040).

One row per site in ``assess_privacy_config``, written by the operator through
``PUT /api/v1/assess/sites/{site}/privacy`` (values supplied by the DPO, OQ-020). A site is
*configured* only when the row exists and every field is within its bounds
(``1 <= retention_days <= 3650``, every text non-empty and within its length, an https notice
URL). The database CHECKs repeat the same bounds, and ``is_configured`` checks them again on
read: a row that does not pass is "not configured", never "configured with a default".

Unset means fail closed: the teacher's enable is refused (plan 11), the Start is refused (the
START gate BR-006 (h), defence in depth), and the setup-check item ``privacy_config`` is
MISSING. Development and CI may let the Start gate pass with ``VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET``
(``VEKTRA_ENV=development`` only); the enable refusal has no such override.

Shortening the retention takes effect at the next purge; lengthening it never brings back what
was purged (the purge deletes, nothing is hidden).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text

RETENTION_MIN_DAYS = 1
RETENTION_MAX_DAYS = 3650

#: field -> (minimum, maximum) length; the bounds of revision 0040's CHECKs
TEXT_BOUNDS: dict[str, tuple[int, int]] = {
    "legal_basis_label": (1, 200),
    "legal_basis_text": (1, 4000),
    "notice_url": (9, 2000),
    "notice_text": (1, 8000),
    "notice_version": (1, 40),
}
FIELDS = ("retention_days", *TEXT_BOUNDS)

#: REQ-089: always part of the pre-session summary, whatever the DPO's texts say
MAILBOX_SENTENCE = (
    "Le copie inviate via email seguono la conservazione della casella di posta universitaria "
    "e non vengono rimosse dalla cancellazione automatica di Vektra."
)


@dataclass(frozen=True)
class PrivacySettings:
    site_id: UUID
    retention_days: int
    legal_basis_label: str
    legal_basis_text: str
    notice_url: str
    notice_text: str
    notice_version: str
    updated_by: str
    updated_at: datetime | None = None


def field_errors(doc: dict[str, Any]) -> list[dict[str, object]]:
    """``{field, code}`` per invalid or missing field (never the value). Empty: valid."""
    errors: list[dict[str, object]] = []
    days = doc.get("retention_days")
    if days is None:
        errors.append({"field": "retention_days", "code": "required"})
    elif not isinstance(days, int) or isinstance(days, bool):
        errors.append({"field": "retention_days", "code": "not_an_integer"})
    elif not RETENTION_MIN_DAYS <= days <= RETENTION_MAX_DAYS:
        errors.append(
            {
                "field": "retention_days",
                "code": "out_of_range",
                "range": [RETENTION_MIN_DAYS, RETENTION_MAX_DAYS],
            }
        )
    for name in TEXT_BOUNDS:
        error = text_field_error(name, doc.get(name))
        if error is not None:
            errors.append(error)
    unknown = sorted(set(doc) - set(FIELDS))
    errors.extend(
        {"field": str(name)[:60], "code": "unknown_field"} for name in unknown
    )
    return errors


def text_field_error(name: str, value: Any) -> dict[str, object] | None:
    """``{field, code}`` when one text setting (a ``TEXT_BOUNDS`` name) is invalid or missing,
    ``None`` when it is valid. The one rule of the stored row, also applied to the notice version
    a mint echoes (plugin request P3)."""
    low, high = TEXT_BOUNDS[name]
    if value is None:
        return {"field": name, "code": "required"}
    if not isinstance(value, str):
        return {"field": name, "code": "not_a_string"}
    if not value.strip():
        return {"field": name, "code": "empty"}
    if len(value) < low:
        return {"field": name, "code": "too_short", "min": low}
    if len(value) > high:
        return {"field": name, "code": "too_long", "max": high}
    if name == "notice_url" and not value.startswith("https://"):
        return {"field": name, "code": "not_https"}
    if name == "notice_url" and not _has_host(value):
        return {"field": name, "code": "invalid_url"}
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
        return {"field": name, "code": "control_character"}
    return None


def _has_host(url: str) -> bool:
    """A URL a browser can follow: it parses and names a host (``https:///x`` does not, and
    ``https://[x`` does not parse: never an exception out of the validation)."""
    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        return False


def passes(row: Any) -> bool:
    """Whether a stored row is within every bound (the read-side check of ``is_configured``)."""
    if row is None:
        return False
    doc = {name: getattr(row, name, None) for name in FIELDS}
    return not field_errors(doc)


READ = text(
    "SELECT site_id, retention_days, legal_basis_label, legal_basis_text, notice_url, "
    "notice_text, notice_version, updated_by, updated_at FROM assess_privacy_config "
    "WHERE site_id = :s"
)


async def read(conn: Any, site_id: UUID) -> PrivacySettings | None:
    """The site's settings when they are complete and within bounds; ``None`` otherwise."""
    if conn is None:
        return None
    row = (await conn.execute(READ, {"s": site_id})).first()
    if not passes(row):
        return None
    return PrivacySettings(
        site_id=row.site_id,
        retention_days=int(row.retention_days),
        legal_basis_label=str(row.legal_basis_label),
        legal_basis_text=str(row.legal_basis_text),
        notice_url=str(row.notice_url),
        notice_text=str(row.notice_text),
        notice_version=str(row.notice_version),
        updated_by=str(row.updated_by),
        updated_at=row.updated_at,
    )


async def is_configured(conn: Any, site_id: UUID) -> bool:
    """REQ-089: every setting set and within bounds. The one definition the START gate, the
    teacher's enable and the setup check use."""
    return await read(conn, site_id) is not None


async def current_notice_version(conn: Any, site_id: UUID) -> str | None:
    """The notice version a Start records (on the session and its audit event); ``None`` when
    the site is not configured."""
    settings = await read(conn, site_id)
    return settings.notice_version if settings is not None else None


async def shown_notice_version(conn: Any, site_id: UUID, fallback: str) -> str:
    """The notice version a student is shown before Start: the site's current one, or the
    deployment value ``fallback`` (``VEKTRA_ASSESS_PRIVACY_NOTICE_VERSION``) that ``GET /state``
    shows for a site with no settings. The one rule of ``/state``, the mint check and Start."""
    return await current_notice_version(conn, site_id) or fallback


def summary_text(settings: PrivacySettings) -> str:
    """The system-rendered pre-session summary (REQ-089): the retention, the legal basis and the
    fixed mailbox sentence, which no setting can remove."""
    return (
        f"I dati di questa discussione sono conservati per {settings.retention_days} giorni "
        f"dalla chiusura e poi cancellati. Base giuridica: {settings.legal_basis_label}. "
        + MAILBOX_SENTENCE
    )


def pre_session_privacy(settings: PrivacySettings) -> dict[str, Any]:
    """The ``pre_session.privacy`` block of ``GET /state``: data plus the one fixed summary."""
    return {
        "notice_version": settings.notice_version,
        "notice_url": settings.notice_url,
        "retention_days": settings.retention_days,
        "legal_basis_label": settings.legal_basis_label,
        "summary": summary_text(settings),
        "mailbox_notice": MAILBOX_SENTENCE,
    }

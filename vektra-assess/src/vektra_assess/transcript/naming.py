"""BR-016 file name and the RFC 6266 ``Content-Disposition`` header.

``<course-shortname>_<activity-id>_<user-id>_s<N>_<yyyymmdd>.html``: identifiers only, never a
full name (OQ-020). The short name is NFKD-folded to ASCII and every character outside
``[A-Za-z0-9-]`` becomes ``-`` (runs collapsed, ends trimmed, at most 40 characters; empty ->
``course``). The date is the CLOSED date in the site time zone.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

_OUTSIDE = re.compile(r"[^A-Za-z0-9-]+")
_DASHES = re.compile(r"-{2,}")
MAX_SHORTNAME = 40
#: the whole name: the pattern a test and the plugin can check
PATTERN = re.compile(r"^[A-Za-z0-9-]{1,40}_\d+_\d+_s\d+_\d{8}\.html$")


def fold_shortname(shortname: str | None) -> str:
    ascii_ = (
        unicodedata.normalize("NFKD", shortname or "")
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    folded = _DASHES.sub("-", _OUTSIDE.sub("-", ascii_)).strip("-")[:MAX_SHORTNAME]
    return folded.strip("-") or "course"


def transcript_filename(
    shortname: str | None,
    cmid: int,
    userid: int,
    session_no: int,
    closed_at: datetime,
    tz: str = "Europe/Rome",
) -> str:
    day = closed_at.astimezone(ZoneInfo(tz)).strftime("%Y%m%d")
    return f"{fold_shortname(shortname)}_{int(cmid)}_{int(userid)}_s{int(session_no)}_{day}.html"


def content_disposition(filename: str) -> str:
    """``attachment`` with an ASCII ``filename=`` and the UTF-8 ``filename*=`` (RFC 6266)."""
    ascii_name = _OUTSIDE.sub("_", filename.replace(".html", "")) + ".html"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"

"""Course-material eligibility for discussion retrieval (FEAT-028, ARCH-072 amendment 2026-11-16).

Pure: it reads the course-wide visibility facts the LMS provider returned
(``LmsProvider.course_modules``) and decides which modules a discussion turn may retrieve from.
A module is ELIGIBLE only when it is visible to every enrolled student by construction:

- its own ``visible`` flag is set and it is shown on the course page (a "stealth" module, available
  but not on the page, is excluded: conservative);
- it has NO availability condition at all (group, grouping, date, completion, profile, nested);
- its section is visible and available to the service account (covers section date and completion
  restrictions; a section GROUP restriction is not readable and is covered by the teacher
  attestation, REQ-077);
- its title does not contain the ingest opt-out tag (``VEKTRA_ASSESS_RAG_OPT_OUT_TAG``, default
  ``[no-ai]``), case-insensitively: the same rule as the course chatbot ingest.

A module absent from the read is not eligible: the allowlist is built from what the read returned,
never from what the index holds. An ``Unreadable`` read gives no allowlist at all (the caller does
no retrieval for that turn).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from vektra_shared.lms import CourseModuleVisibility

DEFAULT_OPT_OUT_TAG = "[no-ai]"
NAMESPACE_MAX = 50
_OUTSIDE = re.compile(r"[^0-9a-z_-]+")
_DASHES = re.compile(r"-{2,}")


@dataclass(frozen=True)
class EligibleModule:
    """One module a turn may retrieve from: its id and the title the prompt shows for it."""

    cmid: int
    title: str


def eligible(module: CourseModuleVisibility, opt_out_tag: str) -> bool:
    """Whether every enrolled student sees ``module`` by construction (ARCH-072 amendment)."""
    if not opt_out_tag:
        # an empty tag would match every title; the setting refuses it, this refuses it too
        raise ValueError("the opt-out tag must not be empty")
    return (
        module.visible
        and module.on_course_page
        and not module.has_conditions
        and module.section_visible
        and module.section_available
        and opt_out_tag.casefold() not in module.title.casefold()
    )


def eligible_modules(
    modules: Iterable[CourseModuleVisibility], opt_out_tag: str
) -> tuple[EligibleModule, ...]:
    """The eligible modules of one read, by cmid, each once (a duplicate cmid in the read is
    eligible only if every copy of it is)."""
    seen: dict[int, EligibleModule | None] = {}
    for m in modules:
        ok = eligible(m, opt_out_tag)
        if m.cmid in seen:
            if not ok or seen[m.cmid] is None:
                seen[m.cmid] = None
            continue
        seen[m.cmid] = EligibleModule(int(m.cmid), m.title) if ok else None
    return tuple(v for _k, v in sorted(seen.items()) if v is not None)


def course_namespace(shortname: str | None, course_id: int) -> str:
    """The Vektra namespace of a course: its short name slugified exactly as the ingest pipeline
    does (the LMS adapter's n8n README, "Namespace Convention"), so the discussion searches the
    namespace the course's material was ingested into: NFD with combining marks stripped,
    lowercase, every character outside ``[0-9a-z_-]`` -> ``-``, runs of ``-`` collapsed, leading
    and trailing ``-`` trimmed, at most 50 characters; an empty result is ``course-<id>``."""
    raw = unicodedata.normalize("NFD", shortname or "")
    raw = "".join(ch for ch in raw if not unicodedata.category(ch).startswith("M"))
    slug = _DASHES.sub("-", _OUTSIDE.sub("-", raw.lower())).strip("-")[:NAMESPACE_MAX]
    return slug or f"course-{int(course_id)}"

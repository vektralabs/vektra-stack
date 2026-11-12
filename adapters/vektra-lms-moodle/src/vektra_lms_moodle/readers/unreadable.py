"""Readers that are EXPECTED to be Unreadable on every supported release (spike, OQ-031).

They are kept as readers so the capability matrix, the setup check and the callers have one
typed answer to ask, and so a future Moodle that adds the function is a one-line change. They
do not call the site: there is no function to call.
"""

from __future__ import annotations

from vektra_assess.moodle.client import SiteRef
from vektra_assess.moodle.result import Reading, Unreadable, UnreadableReason


async def read_module_visibility_for_user(
    site: SiteRef, courseid: int, cmid: int, userid: int
) -> Reading[bool]:
    """Is module ``cmid`` visible to ``userid`` (availability conditions, groups)? OQ-031(1).

    Unreadable on 4.3 and 5.2: no read function takes a target ``userid`` for availability or
    visibility (``core_course_get_contents`` is relative to the CALLER, i.e. the service
    account). Consequence (ARCH-072): RAG over course content stays locked OFF.
    """
    del site, courseid, cmid, userid
    return Unreadable(
        UnreadableReason.FUNCTION_NOT_IN_SERVICE, "no_per_user_visibility_read"
    )


async def read_loginas_events(
    site: SiteRef, courseid: int, userid: int
) -> Reading[bool]:
    """Did someone 'login as' ``userid``? The independent cross-check of REQ-084, OQ-031(2).

    Unreadable on 4.3 and 5.2: no log-reading web service exists (``report_log*`` and
    ``tool_log*`` are absent). The accepted residual risk is trusting the Moodle plugin.
    """
    del site, courseid, userid
    return Unreadable(UnreadableReason.FUNCTION_NOT_IN_SERVICE, "no_log_read_function")

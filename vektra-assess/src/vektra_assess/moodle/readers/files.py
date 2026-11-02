"""Submission file download (plan group C).

``fetch_submission_file`` never trusts the caller's file reference: it re-reads the owner's
submission status first and only downloads a file that status lists, for exactly that owner and
submission (NFR-014: every Moodle id is re-resolved server-side). The bytes stay in memory
(never written to disk) and the download is capped at ``VEKTRA_ASSESS_WS_MAX_FILE_BYTES``.
The pluginfile URL carries the token as a query parameter: it is built inside the client and is
never logged.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from vektra_assess.moodle.client import FILE_PATH, MoodleWsClient, SiteRef
from vektra_assess.moodle.domain import SubmissionFileRef
from vektra_assess.moodle.readers._support import reader
from vektra_assess.moodle.readers.assign import read_submission_status
from vektra_assess.moodle.result import Readable, Reading, Unreadable, UnreadableReason


def _same_file(a: SubmissionFileRef, b: SubmissionFileRef) -> bool:
    return (
        a.filename == b.filename
        and a.filepath == b.filepath
        and a.size == b.size
        and a.mimetype == b.mimetype
        and urlsplit(a.fileurl).path == urlsplit(b.fileurl).path
    )


def _pluginfile_path(fileurl: str) -> str | None:
    """The part of the URL after ``/webservice/pluginfile.php``, or None if it is not one."""
    path = urlsplit(fileurl).path
    if not path.startswith(FILE_PATH + "/"):
        return None
    rest = path[len(FILE_PATH) :]
    if any(seg in ("", ".", "..") for seg in unquote(rest).split("/")[1:]):
        return None
    return rest


@reader
async def fetch_submission_file(
    client: MoodleWsClient,
    site: SiteRef,
    assignid: int,
    file_ref: SubmissionFileRef,
    owner_userid: int,
    submission_id: int,
) -> Reading[bytes]:
    status = await read_submission_status(client, site, assignid, owner_userid)
    if isinstance(status, Unreadable):
        return status
    if (
        status.value.submission_id != submission_id
        or status.value.userid != owner_userid
    ):
        return Unreadable(UnreadableReason.PARTIAL, "submission_mismatch")
    listed = [f for f in status.value.files if _same_file(f, file_ref)]
    if len(listed) != 1:
        return Unreadable(UnreadableReason.PARTIAL, "file_not_in_submission")
    path = _pluginfile_path(listed[0].fileurl)
    if path is None:
        return Unreadable(UnreadableReason.PARTIAL, "file_url_not_pluginfile")
    data = await client.fetch_file(site, path)
    if isinstance(data, Unreadable):
        return data
    if len(data.value) != listed[0].size:
        return Unreadable(UnreadableReason.UNKNOWN_SHAPE, "size_mismatch")
    return Readable(data.value)

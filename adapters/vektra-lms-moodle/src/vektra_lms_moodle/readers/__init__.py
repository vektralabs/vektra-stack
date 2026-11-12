"""Typed Moodle readers: each takes a ``MoodleWsClient``, a ``SiteRef`` and ids, and returns
``Reading[<domain value>]``. No exception escapes a reader."""

from vektra_lms_moodle.readers.assign import (
    read_assign_close_inputs,
    read_assign_release,
    read_assign_settings,
    read_grade_release,
    read_submission_status,
    read_user_flags,
)
from vektra_lms_moodle.readers.enrol import (
    read_course_participants,
    read_editors,
    read_enrolment,
    read_graders,
    read_override_managers,
    read_user_courses,
)
from vektra_lms_moodle.readers.files import fetch_submission_file
from vektra_lms_moodle.readers.grading import (
    read_grading_view,
    read_rubric_display_options,
    read_rubric_fillings,
    read_scale_definition,
)
from vektra_lms_moodle.readers.quiz import (
    read_attempt_review,
    read_quiz_close_inputs,
    read_quiz_overrides,
    read_user_attempts,
    read_user_review_options,
)
from vektra_lms_moodle.readers.resolve import ActivityRef, resolve_activity
from vektra_lms_moodle.readers.site import (
    read_own_auth,
    read_site_info,
    with_release,
)
from vektra_lms_moodle.readers.unreadable import (
    read_loginas_events,
    read_module_visibility_for_user,
)

__all__ = [
    "ActivityRef",
    "fetch_submission_file",
    "read_assign_close_inputs",
    "read_assign_release",
    "read_assign_settings",
    "read_attempt_review",
    "read_course_participants",
    "read_editors",
    "read_enrolment",
    "read_grade_release",
    "read_graders",
    "read_grading_view",
    "read_loginas_events",
    "read_module_visibility_for_user",
    "read_override_managers",
    "read_own_auth",
    "read_quiz_close_inputs",
    "read_quiz_overrides",
    "read_rubric_display_options",
    "read_rubric_fillings",
    "read_scale_definition",
    "read_site_info",
    "read_submission_status",
    "read_user_attempts",
    "read_user_courses",
    "read_user_flags",
    "read_user_review_options",
    "resolve_activity",
    "with_release",
]

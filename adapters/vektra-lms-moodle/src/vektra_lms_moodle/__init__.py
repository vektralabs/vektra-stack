"""The Moodle LMS provider (ADR-0033): typed Moodle web-service readers behind the platform's
``LmsProvider`` Protocol (``vektra_shared.lms``).

Distribution ``vektra-lms-moodle``, entry point ``vektra.lms_providers: moodle``
(``vektra_lms_moodle.provider:build_provider``). It depends on ``vektra-shared`` only: the platform
loads it by name (``VEKTRA_ASSESS_LMS_PROVIDER=moodle``) and no platform package imports it.
Its settings are ``VEKTRA_LMS_MOODLE_*`` (``vektra_lms_moodle.config``).
"""

#: the strict-redaction logger prefix of this provider (``vektra_shared.log_safety``)
LOGGER_PREFIX = "vektra_lms_moodle"

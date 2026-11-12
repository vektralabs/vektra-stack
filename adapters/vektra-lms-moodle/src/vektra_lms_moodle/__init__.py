"""The Moodle LMS provider (ADR-0033): typed Moodle web-service readers behind the platform's
``LmsProvider`` Protocol (``vektra_shared.lms``).

Distribution ``vektra-lms-moodle``, entry point ``vektra.lms_providers: moodle``
(``vektra_lms_moodle.provider:build_provider``). It depends on ``vektra-shared`` only: the platform
loads it by name (``VEKTRA_ASSESS_LMS_PROVIDER=moodle``) and no platform package imports it.
Its settings are ``VEKTRA_LMS_MOODLE_*`` (``vektra_lms_moodle.config``).
"""

from vektra_shared.log_safety import register_strict_logger_prefix

#: the strict-redaction logger prefix of this provider (``vektra_shared.log_safety``). Registered
#: when the package is imported, so every way of building the client is covered (the entry point,
#: a test, the operator CLI): its loggers are not under ``vektra_assess.*`` (ADR-0033 D8). The
#: parser never imports a provider (its import ceiling refuses ``vektra_lms_*``), so this pulls
#: ``vektra_shared.log_safety`` and structlog only into processes that already hold them.
LOGGER_PREFIX = "vektra_lms_moodle"
register_strict_logger_prefix(LOGGER_PREFIX)

"""The operator alerts of an erasure request (OQ-033), with no dependency.

The LMS cannot leave its own privacy request failed, so Vektra tells the operator through an
``operator_notify`` job whose ``kind`` is one of these names (also the ``alert`` of the worker's
``assess.erasure.alert`` log line). ``privacy.erasure`` and ``privacy.purge`` produce them,
``delivery.notify`` reads and renders them: one definition for both sides.
"""

from __future__ import annotations

#: the deletion's verification found rows left: rolled back, the request recorded ``failed``
FAILED = "erasure_verification_failed"
#: still not completed ``VEKTRA_ASSESS_ERASURE_ALERT_AFTER_S`` after the request (marker
#: ``assess_erasure_records.overdue_alerted_at``: once per request)
OVERDUE = "erasure_overdue"
#: still not completed a day after the request: the daily purge records it ``failed``
#: (``error_code`` ``abandoned``)
ABANDONED = "erasure_abandoned"

KINDS = (FAILED, OVERDUE, ABANDONED)
SCOPES = ("subject", "activity")

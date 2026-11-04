"""Advisory-lock keys of the worker's ticks (one stable constant each, collision-tested).

Every tick runs under a session-level ``pg_try_advisory_lock`` held on the control connection, so
two worker processes (an accidental second replica) never run the same tick at once. The key is
a bigint: a fixed 32-bit namespace in the high half (``ASSE``) and a small index in the low half.
The namespace keeps these keys away from any other advisory lock the database sees; the index is
**never renumbered**, only appended, because a second process running an older release must
agree on which key means which tick.
"""

from __future__ import annotations

NAMESPACE = 0x41535345  # "ASSE"


def _key(index: int) -> int:
    return (NAMESPACE << 32) | index


RECONCILE = _key(1)  # startup reconciliation pass
HEARTBEAT = _key(2)
JOB_CLEANUP = _key(3)  # hourly done-row cleanup
SWEEPER = _key(4)  # session deadline / cap / grace (plan session-lifecycle)
STARTING_REAPER = _key(5)
OUTAGE_PROBE = _key(6)
ENROLMENT_RECHECK = _key(7)
GATE_REFRESH = _key(8)
DELIVERY_RETRY = _key(9)  # plan transcript-delivery
PURGE = _key(10)  # plan privacy-retention
SETUP_CHECK = _key(11)  # daily setup-check tick (plan assess-moodle-ws handler)
LEASE_REAPER = _key(12)  # finishes exhausted leases whose audit write failed


def all_keys() -> dict[str, int]:
    """Every constant in this module by name, for the collision test and for operators."""
    return {
        name: value
        for name, value in globals().items()
        if name.isupper() and isinstance(value, int) and name != "NAMESPACE"
    }

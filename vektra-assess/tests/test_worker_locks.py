"""Advisory-lock keys (plan assess-job-loop B6): stable, namespaced, collision-free."""

from __future__ import annotations

import re
from pathlib import Path

from vektra_assess.worker import locks


def test_keys_are_distinct_namespaced_bigints() -> None:
    keys = locks.all_keys()
    assert len(keys) >= 11
    assert len(set(keys.values())) == len(keys), "two ticks would share one lock"
    for name, key in keys.items():
        assert key >> 32 == locks.NAMESPACE, name
        assert 0 < key & 0xFFFFFFFF < 0x10000, name
        assert key < 2**63, "pg advisory keys are signed bigints"


def test_indexes_are_never_renumbered() -> None:
    """A second process on an older release must agree on what a key means: pin the values."""
    pinned = {
        "RECONCILE": 1,
        "HEARTBEAT": 2,
        "JOB_CLEANUP": 3,
        "SWEEPER": 4,
        "SETUP_CHECK": 11,
    }
    keys = locks.all_keys()
    for name, index in pinned.items():
        assert keys[name] & 0xFFFFFFFF == index, name


def test_no_other_module_hardcodes_an_assess_lock_key() -> None:
    src = Path(locks.__file__).resolve().parents[1]
    offenders = [
        f"{p.relative_to(src)}: {m.group(0)}"
        for p in src.rglob("*.py")
        if p.name != "locks.py"
        for m in re.finditer(r"pg_(?:try_)?advisory_lock\(\s*\d+", p.read_text())
    ]
    assert not offenders, offenders

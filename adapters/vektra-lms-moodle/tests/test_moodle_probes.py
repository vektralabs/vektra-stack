"""Capability probes against the recorded 4.3 and 5.2 responses (plan group D, task 3).

Every expectation is derived from the SEED MANIFEST (``derive_probe_manifest``), never from
what Moodle answered. A probe passes only if the reader returns the seeded VALUE: a wrong
expected value must turn the flag unreadable.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from assess_moodle_support import (
    BRANCHES,
    Replay,
    make_client,
    manifest,
    now,
    site_for,
)

from vektra_assess.capabilities import FLAGS
from vektra_assess.moodle.probes import (
    PROBES,
    FlagResult,
    derive_probe_manifest,
    norm,
    run_probes,
)

pytestmark = pytest.mark.parametrize("branch", BRANCHES)

#: what the OQ-025 spike and the notes of plan assess-moodle-ws say, per version
EXPECTED_STATE: dict[str, dict[str, tuple[str, str | None]]] = {
    "4.3": {
        "quiz_overrides": ("unreadable", "function_not_in_service"),
        "per_student_timelimit": ("unreadable", "function_not_in_service"),
    },
    "5.2": {
        "quiz_overrides": ("readable", None),
        "per_student_timelimit": ("readable", None),
    },
}
BOTH: dict[str, tuple[str, str | None]] = {
    "assign_extensions": ("readable", None),
    "assign_cutoff": ("readable", None),
    "assign_user_overrides": ("readable", None),
    "quiz_grace": ("partial", "partial"),
    "attempt_state": ("readable", None),
    "review_options_per_student": ("readable", None),
    "graders_resolvable": ("readable", None),
    "enrolment_readable": ("readable", None),
    "release_state": ("readable", None),
    "grading_state": ("readable", None),
    "review_html_filterable": ("unreadable", "partial"),
    "rubric_display_settings": ("unreadable", "partial"),
    "module_visibility_per_student": ("unreadable", "function_not_in_service"),
    "loginas_crosscheck": ("unreadable", "function_not_in_service"),
}


async def probe(
    branch: str, mutate: Any = None, probe_manifest: dict[str, Any] | None = None
) -> tuple[Replay, dict[str, FlagResult]]:
    replay = Replay(branch)
    if mutate is not None:
        replay.mutate = mutate
    client = make_client(replay)
    pm = probe_manifest or derive_probe_manifest(manifest(branch))
    got = await run_probes(client, site_for(branch), pm, now=now(branch))
    return replay, {r.flag: r for r in got}


def test_one_probe_per_flag(branch: str) -> None:
    assert set(PROBES) == set(FLAGS)
    assert len(FLAGS) == 16


async def test_each_probe_returns_its_expected_state_per_version(branch: str) -> None:
    replay, got = await probe(branch)
    assert list(got) == list(FLAGS)
    want = {**BOTH, **EXPECTED_STATE[branch]}
    assert set(want) == set(FLAGS)
    for flag, (state, reason) in want.items():
        assert (got[flag].state, got[flag].reason_code) == (state, reason), flag
    assert not replay.unmatched
    for r in got.values():
        assert len(r.evidence_hash) == 64 and int(r.evidence_hash, 16) >= 0


async def test_evidence_hash_is_content_free_and_depends_on_the_release(
    branch: str,
) -> None:
    _r, a = await probe(branch)
    _r, b = await probe(branch)
    assert {f: x.evidence_hash for f, x in a.items()} == {
        f: x.evidence_hash for f, x in b.items()
    }
    # a different release is a different hash (and so is a different shape)
    pm = derive_probe_manifest(manifest(branch))
    replay = Replay(branch)
    client = make_client(replay)
    site = site_for(branch)
    other = type(site)(site.site_id, site.base_url, release="9.9.9")
    rows = await run_probes(client, other, pm, now=now(branch))
    assert all(x.evidence_hash != a[x.flag].evidence_hash for x in rows)


def wrong(pm: dict[str, Any], flag: str) -> dict[str, Any]:
    """The same manifest with the FIRST expected value of ``flag`` deliberately wrong."""
    bad = copy.deepcopy(pm)
    check = bad["flags"][flag]["checks"][0]

    def twist(v: Any) -> Any:
        if isinstance(v, bool):
            return not v
        if isinstance(v, int):
            return v + 1
        if v is None:
            return 0
        if isinstance(v, list) and v and isinstance(v[0], list):
            return [twist(v[0]), *v[1:]]
        if isinstance(v, list) and v:
            return [*v[:-1], twist(v[-1])]
        return [0]

    check["expect"] = twist(check["expect"])
    assert norm(check["expect"]) != norm(pm["flags"][flag]["checks"][0]["expect"])
    return bad


VALUE_FLAGS = [
    "assign_extensions",
    "assign_cutoff",
    "assign_user_overrides",
    "attempt_state",
    "review_options_per_student",
    "graders_resolvable",
    "enrolment_readable",
    "release_state",
    "grading_state",
]


@pytest.mark.parametrize("flag", VALUE_FLAGS)
async def test_a_wrong_manifest_value_makes_the_probe_unreadable(
    branch: str, flag: str
) -> None:
    """The probe compares VALUES: the call still returns 200, the flag is still unreadable."""
    pm = wrong(derive_probe_manifest(manifest(branch)), flag)
    replay, got = await probe(branch, probe_manifest=pm)
    assert (got[flag].state, got[flag].reason_code) == ("unreadable", "unknown_shape")
    # and only that flag changed
    _r, good = await probe(branch)
    assert {f: r.state for f, r in got.items() if f != flag} == {
        f: r.state for f, r in good.items() if f != flag
    }
    assert not replay.unmatched


async def test_overrides_are_compared_by_value_on_5_2(branch: str) -> None:
    if branch == "4.3":
        # no override function on 4.3: unreadable by design, whatever the manifest says
        _r, got = await probe(branch)
        assert {
            got[f].reason_code for f in ("quiz_overrides", "per_student_timelimit")
        } == {"function_not_in_service"}
        return
    for flag in ("quiz_overrides", "per_student_timelimit"):
        pm = wrong(derive_probe_manifest(manifest(branch)), flag)
        _r, got = await probe(branch, probe_manifest=pm)
        assert got[flag].state == "unreadable", flag


async def test_a_flag_expected_unreadable_cannot_be_forced_readable(
    branch: str,
) -> None:
    """If a reader became readable where the manifest says it cannot be, that is a mismatch
    to look at, not a capability: unreadable."""
    pm = derive_probe_manifest(manifest(branch))
    pm["flags"]["review_html_filterable"]["checks"][0]["args"]["marker"] = "NOT-THERE"
    _r, got = await probe(branch, probe_manifest=pm)
    assert got["review_html_filterable"].state == "unreadable"
    assert got["review_html_filterable"].reason_code == "unknown_shape"


async def test_grace_is_readable_only_while_the_quiz_is_open(branch: str) -> None:
    """The check follows the clock: past ``open_until`` a quiz must answer partial."""
    pm = derive_probe_manifest(manifest(branch))
    checks = pm["flags"]["quiz_grace"]["checks"]
    assert [c["open_until"] <= now(branch) for c in checks] == [True, False]
    replay = Replay(branch)
    client = make_client(replay)
    # one week later the open quiz is closed too: all partial, still partial overall
    later = now(branch) + 8 * 86400
    # (the recording was made while it was open, so the reader says readable: mismatch)
    rows = await run_probes(client, site_for(branch), pm, now=later)
    assert {r.flag: r for r in rows}["quiz_grace"].state == "unreadable"


async def test_missing_manifest_or_flag_is_not_probed(branch: str) -> None:
    replay = Replay(branch)
    client = make_client(replay)
    rows = await run_probes(client, site_for(branch), None, now=now(branch))
    assert [r.flag for r in rows] == list(FLAGS)
    assert {(r.state, r.reason_code) for r in rows} == {("unreadable", "not_probed")}
    assert replay.calls == []
    pm = derive_probe_manifest(manifest(branch))
    del pm["flags"]["attempt_state"]
    _r, got = await probe(branch, probe_manifest=pm)
    assert (got["attempt_state"].state, got["attempt_state"].reason_code) == (
        "unreadable",
        "not_probed",
    )


async def test_a_raising_reader_is_unreadable_not_an_error(
    branch: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("secret detail")

    monkeypatch.setitem(PROBES, "attempt_state", boom)
    _r, got = await probe(branch)
    assert (got["attempt_state"].state, got["attempt_state"].reason_code) == (
        "unreadable",
        "unknown_shape",
    )


async def test_results_carry_no_id_or_content(branch: str) -> None:
    _r, got = await probe(branch)
    blob = repr(got)
    m = manifest(branch)
    for canary in m["canaries"]:
        assert canary not in blob
    assert "token" not in blob.lower()

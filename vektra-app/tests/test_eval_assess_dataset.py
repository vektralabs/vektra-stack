"""The synthetic NFR-022 dataset meets its minimum sizes and its disjointness rule (plan 7, E)."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "eval"))
sys.path.insert(0, str(ROOT / "tests" / "eval" / "assess"))

import assess_adapter as A  # noqa: E402
import build_dataset as B  # noqa: E402

MANIFEST = A.load_manifest()
PROBES = {
    p.stem: A.load_items(A.ROOT / p.relative_to(A.ROOT))
    for p in sorted((A.ROOT / "probes").glob("*.jsonl"))
}
ALL = [i for items in PROBES.values() for i in items]
FIXTURES = A.load_fixtures()


def test_data_basis_is_synthetic_and_sizes_match_the_files() -> None:
    assert MANIFEST["data_basis"] == "synthetic"
    assert MANIFEST["sizes"] == {k: len(v) for k, v in PROBES.items()}


def test_fixtures_cover_attempts_disciplines_kinds_and_releases() -> None:
    assert len(FIXTURES) >= 10
    kinds = {f.bundle.activity_kind.value for f in FIXTURES.values()}
    assert kinds == {"quiz", "assign"}
    assert {fid.split("-")[1] for fid in FIXTURES} >= {"math", "history"}
    raw = [json.loads(p.read_text()) for p in (A.ROOT / "fixtures").glob("*.json")]
    assert {d["snapshot"]["header"]["lms_release"] for d in raw} == {"4.3", "5.2"}
    items = sum(
        len(f.bundle.quiz_items) + len(f.bundle.criteria) for f in FIXTURES.values()
    )
    assert items >= 100


def test_every_probe_points_at_a_fixture_and_ids_are_unique() -> None:
    assert all(i.fixture in FIXTURES for i in ALL)
    ids = [i.id for i in ALL]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("klass", B.CLASSES)
def test_refusals_per_class_and_language(klass: str) -> None:
    rows = [i for i in PROBES["refusal"] if i.category == f"refusal:{klass}"]
    langs = Counter(i.language for i in rows)
    assert len(rows) >= 20 and langs["it"] > 0 and langs["en"] > 0


def test_contest_injection_distress_and_language_minimums() -> None:
    c = Counter(i.category for i in PROBES["contest"])
    assert c["contest:adversarial"] >= 30 and c["contest:non_adversarial"] >= 30
    assert c["contest:bait"] >= 20 and c["contest:submission_embedded"] >= 10
    assert len(PROBES["injection"]) >= 30
    assert (
        len([i for i in PROBES["refusal"] if i.category == "refusal:W-DISTRESS"]) >= 20
    )
    assert len(PROBES["language"]) >= 3
    assert len(PROBES["part_b"]) == len(FIXTURES)


def test_yes_no_and_oracle_sets_are_disjoint() -> None:
    yesno, oracle = PROBES["yesno"], PROBES["oracle"]
    assert len(yesno) >= 20 and len(oracle) >= 20
    assert not {i.text for i in yesno} & {i.text for i in oracle}
    # yes/no probes target hidden marks only; the oracle set never does
    assert all(FIXTURES[i.fixture].bundle.quiz_items[0].mark is None for i in yesno)
    assert not {i.fixture for i in yesno} & {i.fixture for i in oracle}


def test_the_generator_is_deterministic() -> None:
    import random

    B.RNG = random.Random(20261105)
    fa = B.fixtures()
    a = B.probes(fa)
    B.RNG = random.Random(20261105)
    fb = B.fixtures()
    b = B.probes(fb)
    assert fa == fb  # the fixture documents too, not only the probes
    assert a == b

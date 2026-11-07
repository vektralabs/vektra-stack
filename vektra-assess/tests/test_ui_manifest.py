"""Review #9: the UI bundle's manifest is read once per directory, not on every request."""

from __future__ import annotations

import json
from pathlib import Path

from vektra_assess.ui import router


def _write(d: Path, js: str) -> None:
    (d / "manifest.json").write_text(json.dumps({"js": js, "css": "a.css"}))


def test_the_manifest_is_read_once_per_directory(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    reads: list[Path] = []
    real = Path.read_text

    def counting(self: Path, *a, **k):  # type: ignore[no-untyped-def]
        if self.name == "manifest.json":
            reads.append(self)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting)
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    _write(one, "x.js")
    _write(two, "y.js")
    for _ in range(50):
        assert router.manifest_of(one) == {"js": "x.js", "css": "a.css"}
    _write(one, "changed.js")  # a new image is a new process: never re-read in this one
    assert router.manifest_of(one)["js"] == "x.js"
    assert router.manifest_of(two)["js"] == "y.js"  # another directory, its own entry
    assert reads == [one / "manifest.json", two / "manifest.json"]


def test_a_missing_bundle_is_none(tmp_path: Path) -> None:
    assert router.manifest_of(tmp_path / "nothing") is None

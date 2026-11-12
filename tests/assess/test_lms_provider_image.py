"""The ``lms-moodle`` image variant holds exactly the pinned provider wheel (plan 2b.2, 2b.3).

The provider is released from its own repository (vektra-moodle-discuss, ``provider/``) and the
variant installs that wheel by version and sha256 from ``docker/lms-providers/moodle.txt``
(ADR-0033 D6, OD-6). The static guard (``vektra-shared/tests/test_image_content_boundary.py``)
proves the Dockerfile installs that pin and nothing else, with ``--require-hashes``. This module
looks at the built image itself, ``vektra-stack-lms-moodle`` (what ``docker-compose.assess.yml``
builds and every assess service runs):

- the provider installed in it is the pinned version, from the pinned URL, and every file of its
  package has the sha256 the pinned wheel's own RECORD lists, after the wheel downloaded here was
  checked against the pinned sha256: the image holds that wheel's bytes, not a rebuild of them;
- the platform's own loader finds ``moodle`` in ``vektra.lms_providers``, builds it, and the
  object implements the ``LmsProvider`` Protocol (the import-time conformance a worker relies on);
- it is the only provider in the image.

The ``assess-stack`` CI job builds the image and sets VEKTRA_ASSESS_STACK=1: a missing image is
then a FAILURE (BLOCKED), never a skip. Without it (a developer run with no image) it skips.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import urllib.request
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
PIN = ROOT / "docker/lms-providers/moodle.txt"
IMAGE = os.environ.get("VEKTRA_LMS_MOODLE_IMAGE", "vektra-stack-lms-moodle")
DIST = "vektra-lms-moodle"
PACKAGE = "vektra_lms_moodle"
_PIN_LINE = re.compile(
    r"^(?P<dist>[a-z0-9-]+) @ (?P<url>https://\S+/(?P<file>[^/\s]+\.whl))"
    r" --hash=sha256:(?P<sha256>[0-9a-f]{64})$"
)
_WHEEL_FILE = re.compile(r"^(?P<pkg>[a-z0-9_]+)-(?P<version>[^-]+)-py3-none-any\.whl$")


def pinned() -> dict[str, str]:
    lines = [
        ln.strip()
        for ln in PIN.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert len(lines) == 1, lines
    m = _PIN_LINE.match(lines[0])
    assert m, lines[0]
    w = _WHEEL_FILE.match(m["file"])
    assert w, m["file"]
    return {**m.groupdict(), "version": w["version"]}


# Runs inside the image (python of the venv the runtime stage copied).
_PROBE = r"""
import asyncio, base64, hashlib, json
from importlib.metadata import distribution, entry_points

dist = distribution("vektra-lms-moodle")
files = {}
for f in dist.files or ():
    name = str(f)
    if name.startswith("vektra_lms_moodle/") and not name.endswith(".pyc"):
        digest = hashlib.sha256(f.locate().read_bytes()).digest()
        files[name] = "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
direct = json.loads(dist.read_text("direct_url.json") or "null")
eps = sorted(
    (e.name, e.value, e.dist.name if e.dist else None)
    for e in entry_points(group="vektra.lms_providers")
)

from vektra_app.lms_providers import available_lms_providers, load_lms_provider
from vektra_shared.lms import LmsProvider

provider = asyncio.run(load_lms_provider("moodle", development=False))
try:
    loaded = {
        "type": type(provider).__module__ + "." + type(provider).__name__,
        "is_lms_provider": isinstance(provider, LmsProvider),
        "name": provider.name,
    }
finally:
    asyncio.run(provider.aclose())
print(json.dumps({
    "version": dist.version,
    "direct_url": direct,
    "files": files,
    "entry_points": eps,
    "available": list(available_lms_providers()),
    "loaded": loaded,
}))
"""


@pytest.fixture(scope="module")
def image_report() -> dict[str, object]:
    found = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, text=True
    )
    if found.returncode != 0:
        msg = f"image {IMAGE} not built (docker compose -f docker-compose.yml -f docker-compose.assess.yml build vektra)"
        if os.environ.get("VEKTRA_ASSESS_STACK") == "1":
            pytest.fail(f"BLOCKED: {msg}")
        pytest.skip(msg)
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", IMAGE, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert out.returncode == 0, out.stderr[-4000:]
    report: dict[str, object] = json.loads(out.stdout.strip().splitlines()[-1])
    return report


@pytest.fixture(scope="module")
def pinned_wheel_record() -> dict[str, str]:
    """The pinned wheel, downloaded and checked against the pinned sha256, then its RECORD:
    ``{path: "sha256=<urlsafe b64>"}`` for every file of the package."""
    pin = pinned()
    with urllib.request.urlopen(pin["url"], timeout=60) as resp:
        data = resp.read()
    assert hashlib.sha256(data).hexdigest() == pin["sha256"], (
        "the released wheel is not the pinned one"
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        (record,) = [n for n in zf.namelist() if n.endswith(".dist-info/RECORD")]
        rows = list(csv.reader(io.StringIO(zf.read(record).decode())))
    return {
        path: digest
        for path, digest, *_ in rows
        if path.startswith(f"{PACKAGE}/") and digest
    }


def test_the_pin_is_one_hashed_wheel_of_the_provider() -> None:
    pin = pinned()
    assert pin["dist"] == DIST
    assert pin["file"].startswith(f"{PACKAGE}-{pin['version']}-")
    assert f"/provider-v{pin['version']}/" in pin["url"], pin["url"]


def test_the_variant_holds_exactly_the_pinned_wheel(
    image_report: dict[str, object], pinned_wheel_record: dict[str, str]
) -> None:
    pin = pinned()
    assert image_report["version"] == pin["version"]
    direct = image_report["direct_url"]
    assert isinstance(direct, dict) and direct["url"] == pin["url"], direct
    files = image_report["files"]
    assert isinstance(files, dict)
    assert len(pinned_wheel_record) > 30, "the wheel's RECORD lists too few files"
    # every file of the installed package is a file of the pinned wheel, byte for byte
    assert files == pinned_wheel_record


def test_the_platform_loader_builds_the_provider(
    image_report: dict[str, object],
) -> None:
    assert image_report["entry_points"] == [
        ["moodle", "vektra_lms_moodle.provider:build_provider", DIST]
    ]
    assert image_report["available"] == ["moodle"]
    assert image_report["loaded"] == {
        "type": "vektra_lms_moodle.provider.MoodleLmsProvider",
        "is_lms_provider": True,
        "name": "moodle",
    }

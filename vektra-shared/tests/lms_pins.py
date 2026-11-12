"""The LMS provider pins of the image variants (ADR-0033 D6, OD-6; plan 2b.2, 2b.3): one parser.

Each provider an image variant can install has one pin file, ``docker/lms-providers/<name>.txt``,
holding one requirement: the provider's released wheel by https URL, with its sha256. This module
is the only reader of those files for the guards: ``test_image_content_boundary.py``,
``test_lms_boundary.py`` and ``tests/assess/test_lms_provider_image.py`` load it by path (it is
stdlib only, so it also runs as a script in CI: ``python3 vektra-shared/tests/lms_pins.py tag
moodle`` prints the provider release tag the ``test-lms-provider`` job checks out).

It also says what a provider wheel may contain: its package and its ``.dist-info`` only. A wheel
installs every top-level entry it carries into the image's site-packages, so an extra top-level
directory could shadow a platform package (``vektra_shared/``), and test trees or recordings
would ship test assets in a production image.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PIN_DIR = REPO_ROOT / "docker" / "lms-providers"
DIST_PREFIX = "vektra-lms-"
#: path segments that mark test material, inside the provider's package too
TEST_SEGMENTS = frozenset(
    {"tests", "test", "fixtures", "canary", "moodle_ws", "recordings"}
)

_PIN_LINE = re.compile(
    r"^(?P<dist>[a-z0-9-]+) @ "
    r"(?P<url>https://github\.com/vektralabs/[a-z0-9-]+/releases/download/(?P<tag>[^/\s]+)/"
    r"(?P<file>[^/\s]+\.whl)) --hash=sha256:(?P<sha256>[0-9a-f]{64})$"
)


@dataclass(frozen=True)
class Pin:
    name: str  # the pin file's stem, the INSTALL_LMS_PROVIDER value
    dist: str  # vektra-lms-<name>
    module: str  # vektra_lms_<name>
    version: str
    url: str
    tag: str
    file: str
    sha256: str


def parse_pin(text: str, name: str) -> Pin:
    """The one requirement of the pin file ``<name>.txt``. Refused (``ValueError``): no or a
    non-sha256 hash, more than one requirement, a by-name requirement, http, a host other than a
    vektralabs GitHub release, a distribution other than ``vektra-lms-<name>``, a wheel of
    another package or not pure, a release tag that does not carry the wheel's version."""
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if len(lines) != 1:
        raise ValueError(
            f"a pin file holds exactly one requirement, found {len(lines)}"
        )
    m = _PIN_LINE.match(lines[0])
    if not m:
        raise ValueError(f"not a hashed wheel URL of a vektralabs release: {lines[0]}")
    dist, module = f"{DIST_PREFIX}{name}", f"{DIST_PREFIX}{name}".replace("-", "_")
    if m["dist"] != dist:
        raise ValueError(f"{name}.txt pins {m['dist']}, not {dist}")
    w = re.fullmatch(re.escape(module) + r"-([^-]+)-py3-none-any\.whl", m["file"])
    if not w:
        raise ValueError(f"the wheel {m['file']} is not a pure wheel of {dist}")
    version = w.group(1)
    if not m["tag"].endswith(f"v{version}"):
        raise ValueError(f"release tag {m['tag']} does not carry version {version}")
    return Pin(name, dist, module, version, m["url"], m["tag"], m["file"], m["sha256"])


def pins(pin_dir: Path = PIN_DIR) -> dict[str, Pin]:
    """Every pin file, parsed (a malformed one raises)."""
    return {
        p.stem: parse_pin(p.read_text(encoding="utf-8"), p.stem)
        for p in sorted(pin_dir.glob("*.txt"))
    }


def wheel_layout_problems(paths: list[str], module: str, version: str) -> list[str]:
    """What a provider wheel (its archive names, or an installed distribution's files) carries
    beyond its package and its ``.dist-info``, and any test material inside them."""
    allowed = {module, f"{module}-{version}.dist-info"}
    problems = []
    for path in sorted(paths):
        parts = path.replace("\\", "/").split("/")
        if parts[0] not in allowed:
            problems.append(
                f"{path}: top-level entry {parts[0]!r} is not {sorted(allowed)}"
            )
        elif TEST_SEGMENTS & set(parts[:-1]) or parts[-1].startswith(
            ("test_", "conftest")
        ):
            problems.append(f"{path}: test material")
    return problems


if __name__ == "__main__":  # python3 lms_pins.py <field> <name>
    field, name = sys.argv[1], sys.argv[2]
    print(getattr(pins()[name], field))

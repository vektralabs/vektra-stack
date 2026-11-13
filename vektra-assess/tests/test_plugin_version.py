"""The plugin version check at mint (``api/deps.py``, ARCH-067): numeric per component, never
lexical; a ``-``/``+`` suffix is ignored (so a pre-release of X counts as X); junk and an
unparsable minimum fail closed."""

from __future__ import annotations

import pytest

from vektra_assess.api.deps import version_at_least
from vektra_assess.config import VektraAssessSettings


def test_the_default_minimum_is_0_2_0() -> None:
    """``local_vektradiscuss`` 0.2.0 always sends the transcript routes' ``cmid`` (now
    required) and the Start mint's ``privacy_notice_version``."""
    field = VektraAssessSettings.model_fields["min_plugin_version"]
    assert field.default == "0.2.0"


@pytest.mark.parametrize(
    ("raw", "minimum", "ok"),
    [
        ("0.2.0", "0.2.0", True),  # equal is enough
        ("0.10.0", "0.2.0", True),  # 10 > 2, not "1" < "2"
        ("0.2.1", "0.2.0", True),
        ("1.0.0", "0.2.0", True),
        ("0.2", "0.2.0", True),  # missing components are zeros
        ("0.1.9", "0.2.0", False),
        ("0.1.10", "0.2.0", False),
        ("0.0.9", "0.2.0", False),
        ("0.2.0-rc1", "0.2.0", True),  # the suffix is ignored (current rule)
        ("0.2.0+build.7", "0.2.0", True),
        ("0.1.9-rc1", "0.2.0", False),
        ("v0.2.0", "0.2.0", False),  # junk fails closed
        ("", "0.2.0", False),
        ("0.2.0", "banana", False),  # an unparsable minimum fails closed
    ],
)
def test_version_at_least(raw: str, minimum: str, ok: bool) -> None:
    assert version_at_least(raw, minimum) is ok


def test_every_minimum_applies_and_none_is_skipped() -> None:
    """The deployment's minimum and the site's own: the higher one decides."""
    assert version_at_least("0.3.0", "0.2.0", None)
    assert not version_at_least("0.3.0", "0.2.0", "0.4.0")
    assert version_at_least("0.4.0", "0.2.0", "0.4.0")


@pytest.mark.parametrize(
    "bad", ["0.1.0", "0.1.9", "0.1.99-rc1", "0", "banana", "v0.2.0"]
)
def test_a_minimum_below_the_contract_floor_refuses_the_settings(bad: str) -> None:
    """Review #1: 0.2.0 is the contract floor, not a free knob. The transcript routes require
    the page ``cmid``, which no release before 0.2.0 sends; a lower (or unparsable) minimum
    would admit a plugin whose every transcript call is a 400."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        VektraAssessSettings.model_validate({"VEKTRA_ASSESS_MIN_PLUGIN_VERSION": bad})
    msg = str(exc.value)
    assert "VEKTRA_ASSESS_MIN_PLUGIN_VERSION" in msg
    assert "0.2.0" in msg and "cmid" in msg


@pytest.mark.parametrize("good", ["0.2.0", "0.2.1", "0.10.0", "1.0.0", "0.2.0-rc1"])
def test_a_minimum_at_or_above_the_floor_is_accepted(good: str) -> None:
    got = VektraAssessSettings.model_validate(
        {"VEKTRA_ASSESS_MIN_PLUGIN_VERSION": good}
    )
    assert got.min_plugin_version == good

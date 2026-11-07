"""Plan 9 review (#2): ONE origin normalisation for the migration, the router, startup and the
setup check (``vektra_assess.ui.origins.bare_origin``)."""

from __future__ import annotations

import pytest

from vektra_assess.ui.origins import (
    bare_origin,
    site_origin,
    ui_origin_problem,
    url_origin,
)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("https://m.example", "https://m.example"),
        ("HTTPS://M.Example", "https://m.example"),
        ("https://m.example:443", "https://m.example"),
        ("HTTPS://M.EXAMPLE:443/", "https://m.example"),
        ("http://m.example:80", "http://m.example"),
        ("http://m.example:443", "http://m.example:443"),
        ("https://m.example:8443", "https://m.example:8443"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("https://[::1]:8443", "https://[::1]:8443"),
        ("https://[0:0:0:0:0:0:0:1]:443", "https://[::1]"),
        ("https://m.example.", "https://m.example"),
    ],
)
def test_bare_origin_normalises(raw: str, want: str) -> None:
    assert bare_origin(raw) == want


@pytest.mark.parametrize(
    "raw",
    [
        "https://m.example/moodle",
        "https://u:p@m.example",
        "https://u@m.example",
        "https://*.example",
        "ftp://m.example",
        "https://m_x.example",  # not a hostname: the CHECK of revision 0033 refuses it too
        "https://m.example:99999",
        "https://m.example?x=1",
        "https://",
        "",
    ],
)
def test_bare_origin_refuses(raw: str) -> None:
    with pytest.raises(ValueError):
        bare_origin(raw)


def test_url_origin_is_bare_origin_of_the_scheme_and_authority() -> None:
    assert url_origin("HTTPS://M.Example:443/moodle/") == "https://m.example"
    assert url_origin("https://[::1]:8443/moodle") == "https://[::1]:8443"
    assert (
        url_origin("https://u:p@m.example/moodle") is None
    )  # credentials: not normalisable
    assert url_origin("https://m_x.example/") is None
    assert url_origin("not a url") is None


def test_site_origin_prefers_the_column_and_falls_back_to_base_url() -> None:
    assert site_origin("HTTPS://Moodle.Example:443", "https://other.example/") == (
        "https://moodle.example"
    )
    assert site_origin(None, "HTTPS://M.Example:443/moodle") == "https://m.example"
    assert site_origin("", "https://m.example/x") == "https://m.example"
    assert site_origin("https://u:p@m.example", "https://m.example/") is None


def test_the_distinct_check_compares_normalised_origins() -> None:
    # case and the default port do not make two origins distinct
    assert (
        ui_origin_problem(
            "HTTPS://M.EXAMPLE:443", ["https://m.example"], development=False
        )
        == "ui_origin_equals_moodle_origin"
    )
    assert (
        ui_origin_problem(
            "https://ui.example", ["HTTPS://M.Example:443"], development=False
        )
        is None
    )


def test_the_nginx_example_domains_pass_the_same_site_items() -> None:
    """The two public origins the nginx example documents (API and UI) and its Moodle example
    pass the setup check's same-site items (moved here from the shared tests, CodeRabbit
    4501067261: the shared tests import no assess code)."""
    import re
    from pathlib import Path

    from vektra_assess.moodle.setup_check import same_site

    conf = (
        Path(__file__).resolve().parents[2] / "deploy/nginx/vektra.conf.example"
    ).read_text()
    examples = dict(re.findall(r"#\s+(YOUR_\w+|MOODLE)\s+e\.g\.\s+(\S+)", conf))
    ui_origin = f"https://{examples['YOUR_UI_DOMAIN']}"
    assert not same_site(
        ui_origin, f"https://{examples['YOUR_DOMAIN']}"
    )  # same_site_as_api
    assert not same_site(
        ui_origin, f"https://{examples['MOODLE']}"
    )  # same_site_as_moodle

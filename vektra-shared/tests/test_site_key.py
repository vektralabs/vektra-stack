"""The one normalisation of an LMS site's root URL (FEAT-028 review 1, metadata ``lms_site``)."""

from __future__ import annotations

from typing import Any

import pytest

from vektra_shared.site_key import lms_site_key


@pytest.mark.parametrize(
    ("base_url", "key"),
    [
        ("HTTPS://Moodle.Example.org:443/lms/", "https://moodle.example.org/lms"),
        ("http://h:8052", "http://h:8052"),
        ("https://Moodle.Example.IT/", "https://moodle.example.it"),
        ("https://moodle.example.it:443/moodle/", "https://moodle.example.it/moodle"),
        ("http://localhost:8080/moodle", "http://localhost:8080/moodle"),
        ("HTTP://Host:80", "http://host"),
        ("https://host:80/x", "https://host:80/x"),  # 80 is not https's default
        ("https://host/a/B?x=1#f", "https://host/a/B"),  # the path keeps its case
        ("https://host/a//", "https://host/a"),
        ("  https://host/  ", "https://host"),
        ("ftp://host", None),
        ("not a url", None),
        ("https://", None),
        ("https://host:99999", None),
        ("", None),
        (None, None),
        ("https://user:pw@host", None),
    ],
)
def test_the_site_key_is_the_normalised_root_url(
    base_url: Any, key: str | None
) -> None:
    assert lms_site_key(base_url) == key

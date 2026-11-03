"""vektra_shared.netguard: allowlists, all-address validation, pinning, rebinding."""

from __future__ import annotations

import socket

import pytest

from vektra_shared.netguard import EgressDenied, resolve_pinned


def fake(*ips: str):
    """A resolver that answers with the given addresses and counts its calls."""
    calls: list[str] = []

    def resolver(host, port, *_a, **_k):
        calls.append(host)
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (ip, port, 0, 0) if ":" in ip else (ip, port),
            )
            for ip in ips
        ]

    resolver.calls = calls  # type: ignore[attr-defined]
    return resolver


def test_public_host_is_pinned_with_sni_and_host_header():
    r = fake("93.184.216.34")
    t = resolve_pinned(
        "https://moodle.example.org/webservice/rest/server.php?a=1",
        allowed_hosts=["moodle.example.org"],
        resolver=r,
    )
    assert t.ip == "93.184.216.34" and t.sni_hostname == "moodle.example.org"
    assert t.pinned_url == "https://93.184.216.34/webservice/rest/server.php?a=1"
    assert t.host_header == "moodle.example.org"


def test_non_default_port_is_kept():
    t = resolve_pinned(
        "http://moodle.example.org:8080/x",
        allowed_hosts=["moodle.example.org"],
        deny_private=False,
        resolver=fake("10.1.2.3"),
    )
    assert (
        t.pinned_base == "http://10.1.2.3:8080"
        and t.host_header == "moodle.example.org:8080"
    )


def test_resolution_happens_once_and_the_result_is_immutable():
    """Rebinding: a second, different DNS answer is never consulted."""
    r = fake("93.184.216.34")
    t = resolve_pinned(
        "https://m.example.org/", allowed_hosts=["m.example.org"], resolver=r
    )
    assert r.calls == ["m.example.org"]  # exactly one lookup
    with pytest.raises(AttributeError):
        t.ip = "127.0.0.1"  # type: ignore[misc]


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.9", "::1", "fd00::1"]
)
def test_private_addresses_are_denied_by_default(ip):
    with pytest.raises(EgressDenied, match="private or reserved"):
        resolve_pinned(
            "https://m.example.org/", allowed_hosts=["m.example.org"], resolver=fake(ip)
        )


@pytest.mark.parametrize(
    "ip", ["169.254.169.254", "0.0.0.0", "224.0.0.1", "fe80::1", "::"]
)
def test_always_denied_even_with_deny_private_off_or_a_cidr(ip):
    with pytest.raises(EgressDenied):
        resolve_pinned(
            "https://m.example.org/",
            allowed_hosts=["m.example.org"],
            deny_private=False,
            resolver=fake(ip),
        )
    with pytest.raises(EgressDenied):
        resolve_pinned(
            "https://m.example.org/",
            allowed_hosts=["m.example.org"],
            allowed_cidrs=["0.0.0.0/0", "::/0"],
            resolver=fake(ip),
        )


def test_every_address_is_validated_not_only_the_first():
    r = fake("93.184.216.34", "10.0.0.5")
    with pytest.raises(EgressDenied):
        resolve_pinned(
            "https://m.example.org/", allowed_hosts=["m.example.org"], resolver=r
        )
    r = fake("93.184.216.34", "169.254.169.254")
    with pytest.raises(EgressDenied):
        resolve_pinned(
            "https://m.example.org/",
            allowed_hosts=["m.example.org"],
            deny_private=False,
            resolver=r,
        )


def test_cidr_allowlist_opens_exactly_that_private_range():
    ok = resolve_pinned(
        "https://m.example.org/",
        allowed_hosts=["m.example.org"],
        allowed_cidrs=["10.20.0.0/16"],
        resolver=fake("10.20.3.4"),
    )
    assert ok.ip == "10.20.3.4"
    with pytest.raises(EgressDenied, match="outside the allowed networks"):
        resolve_pinned(
            "https://m.example.org/",
            allowed_hosts=["m.example.org"],
            allowed_cidrs=["10.20.0.0/16"],
            resolver=fake("10.21.0.1"),
        )
    with pytest.raises(EgressDenied):  # one good and one outside address
        resolve_pinned(
            "https://m.example.org/",
            allowed_hosts=["m.example.org"],
            allowed_cidrs=["10.20.0.0/16"],
            resolver=fake("10.20.3.4", "93.184.216.34"),
        )


def test_ipv6_is_pinned_in_brackets_and_v4_mapped_is_normalised():
    t = resolve_pinned(
        "https://m.example.org/p",
        allowed_hosts=["m.example.org"],
        resolver=fake("2606:2800:220:1:248:1893:25c8:1946"),
    )
    assert t.pinned_url == "https://[2606:2800:220:1:248:1893:25c8:1946]/p"
    with pytest.raises(EgressDenied):  # ::ffff:10.0.0.1 is the private 10.0.0.1
        resolve_pinned(
            "https://m.example.org/",
            allowed_hosts=["m.example.org"],
            resolver=fake("::ffff:10.0.0.1"),
        )


def test_host_allowlist_is_required_and_case_insensitive():
    with pytest.raises(EgressDenied, match="not on the allowlist"):
        resolve_pinned(
            "https://evil.example.org/",
            allowed_hosts=["m.example.org"],
            resolver=fake("93.184.216.34"),
        )
    with pytest.raises(EgressDenied, match="no allowed hosts"):
        resolve_pinned(
            "https://m.example.org/", allowed_hosts=[], resolver=fake("93.184.216.34")
        )
    t = resolve_pinned(
        "https://M.Example.ORG./",
        allowed_hosts=["m.example.org"],
        resolver=fake("93.184.216.34"),
    )
    assert t.ip == "93.184.216.34"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://m.example.org/",
        "file:///etc/passwd",
        "https://u:p@m.example.org/",
        "https:///x",
        "https://m.example.org:notaport/",
    ],
)
def test_malformed_or_credentialed_urls_are_refused(url):
    with pytest.raises(EgressDenied):
        resolve_pinned(
            url, allowed_hosts=["m.example.org"], resolver=fake("93.184.216.34")
        )


def test_resolution_failure_and_empty_answer_are_refused_without_leaking_the_query():
    def boom(*_a, **_k):
        raise socket.gaierror("nope")

    with pytest.raises(EgressDenied, match="cannot resolve"):
        resolve_pinned(
            "https://m.example.org/?wstoken=SECRET",
            allowed_hosts=["m.example.org"],
            resolver=boom,
        )
    with pytest.raises(EgressDenied, match="cannot resolve"):
        resolve_pinned(
            "https://m.example.org/",
            allowed_hosts=["m.example.org"],
            resolver=lambda *a, **k: [],
        )
    try:
        resolve_pinned(
            "https://evil.org/?wstoken=SECRET",
            allowed_hosts=["m.example.org"],
            resolver=fake("1.1.1.1"),
        )
    except EgressDenied as exc:
        assert "SECRET" not in str(exc)


@pytest.mark.parametrize(
    ("cidr", "ip"),
    [("127.0.0.0/8", "127.0.0.1"), ("::1/128", "::1"), ("0.0.0.0/0", "127.0.0.9")],
)
def test_loopback_is_denied_even_inside_an_allowed_cidr_unless_development(cidr, ip):
    kw = {
        "allowed_hosts": ["m.example.org"],
        "allowed_cidrs": [cidr],
        "resolver": fake(ip),
    }
    with pytest.raises(EgressDenied, match="loopback"):
        resolve_pinned("https://m.example.org/", **kw)
    t = resolve_pinned("https://m.example.org/", allow_loopback=True, **kw)
    assert t.ip == ip


def test_allow_loopback_does_not_open_other_ranges_or_metadata():
    with pytest.raises(EgressDenied, match="outside the allowed networks"):
        resolve_pinned(
            "https://m.example.org/",
            allowed_hosts=["m.example.org"],
            allowed_cidrs=["127.0.0.0/8"],
            allow_loopback=True,
            resolver=fake("10.0.0.1"),
        )
    with pytest.raises(EgressDenied, match="link-local"):
        resolve_pinned(
            "https://m.example.org/",
            allowed_hosts=["m.example.org"],
            allowed_cidrs=["0.0.0.0/0"],
            allow_loopback=True,
            resolver=fake("169.254.169.254"),
        )

"""Egress guard: resolve once, validate every address, pin the connection to one IP.

A generic primitive for any client that calls out to an operator-registered host (ADR-0033 D8):
an LMS provider's web-service client is its first user (ARCH-070). It is a COPY of the
idea in ``vektra_learn.api._resolve_and_validate_url``, not a move: learn denies every
private address (right for a public document URL), while an on-prem LMS usually sits on a
private address, so an allowlist policy is needed instead. Learn is untouched; unifying
the two (one implementation, the policy as a parameter) is DEBT-042 in ``.s2s/BACKLOG.md``.

Policy, applied to EVERY address the name resolves to (not only the first):

- ``allowed_hosts``: the URL host must be one of them (case-insensitive). Required.
- ``allowed_cidrs``: when given, every address must lie inside one of them. This is how an
  operator allows a private LMS host (``10.20.0.0/16``) without opening all of RFC 1918.
- ``deny_private`` (default True): without CIDRs, private/loopback/reserved addresses are
  refused.
- always refused, whatever the settings: unspecified, multicast and link-local addresses
  (which includes the 169.254.169.254 cloud metadata endpoint).

The caller connects to ``PinnedTarget.ip`` and keeps ``sni_hostname`` for TLS verification
and the Host header, so the certificate is still checked against the real name. The target is
immutable and resolution happens once: a second, different DNS answer (rebinding) is never
consulted.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

Resolver = Callable[..., list[tuple[Any, Any, Any, Any, tuple[Any, ...]]]]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class EgressDenied(ValueError):
    """The URL is not allowed. The message never echoes a query string or credentials."""


class ResolutionFailed(EgressDenied):
    """The name did not resolve: an infrastructure failure, not a policy decision."""


@dataclass(frozen=True)
class PinnedTarget:
    scheme: str
    host: str
    port: int
    ip: str
    path: str  # path plus ?query, as sent on the request line
    pinned_base: str  # scheme://ip[:port], what the HTTP client connects to
    sni_hostname: str  # the original host, for TLS SNI/verification and the Host header

    @property
    def pinned_url(self) -> str:
        return self.pinned_base + self.path

    @property
    def host_header(self) -> str:
        default = 443 if self.scheme == "https" else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"


def _normalise(ip: IPAddress) -> IPAddress:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _always_denied(ip: IPAddress) -> str | None:
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_link_local:
        return "link-local address (includes the cloud metadata endpoint)"
    return None


def resolve_pinned(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    allowed_cidrs: Iterable[str] = (),
    deny_private: bool = True,
    allow_loopback: bool = False,
    resolver: Resolver = socket.getaddrinfo,
) -> PinnedTarget:
    """Validate ``url`` and pin it to one resolved address.

    ``allowed_hosts`` is the caller's allowlist. A caller that builds the URL from a
    registered base URL and passes that URL's own host (as an LMS provider's client does)
    makes this check tautological ON PURPOSE: the registered URL is written only by
    operator provisioning, so the host is trusted by construction and the real guard here
    is the address check below.

    A CIDR allowlist opens exactly the networks it names, but loopback is never among
    them unless ``allow_loopback`` is True (development only): an ``allowed_cidrs`` that
    happens to cover 127.0.0.0/8 or a wide internal range does not make the machine's own
    services reachable. Unspecified, multicast and link-local (cloud metadata) addresses
    are always refused.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise EgressDenied("only http and https URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise EgressDenied("credentials in the URL are not allowed")
    host = parts.hostname
    if not host:
        raise EgressDenied("URL has no host")
    hosts = {h.lower().rstrip(".") for h in allowed_hosts}
    if not hosts:
        raise EgressDenied("no allowed hosts configured")
    if host.lower().rstrip(".") not in hosts:
        raise EgressDenied(f"host {host!r} is not on the allowlist")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise EgressDenied("invalid port") from None
    networks: list[IPNetwork] = [
        ipaddress.ip_network(c, strict=False) for c in allowed_cidrs
    ]

    try:
        infos = resolver(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError as exc:
        raise ResolutionFailed(
            f"cannot resolve {host!r}: {type(exc).__name__}"
        ) from None
    if not infos:
        raise ResolutionFailed(f"cannot resolve {host!r}")

    addresses: list[IPAddress] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = _normalise(ipaddress.ip_address(sockaddr[0].split("%")[0]))
        if reason := _always_denied(ip):
            raise EgressDenied(f"{host!r} resolves to a {reason}")
        if networks:
            if ip.is_loopback and not allow_loopback:
                raise EgressDenied(
                    f"{host!r} resolves to a loopback address (not allowed even "
                    "inside an allowed network)"
                )
            if not any(ip in net for net in networks):
                raise EgressDenied(f"{host!r} resolves outside the allowed networks")
        elif deny_private and (not ip.is_global or ip.is_loopback or ip.is_reserved):
            raise EgressDenied(f"{host!r} resolves to a private or reserved address")
        addresses.append(ip)

    chosen = addresses[0]
    ip_text = f"[{chosen}]" if chosen.version == 6 else str(chosen)
    default = 443 if parts.scheme == "https" else 80
    netloc = ip_text if port == default else f"{ip_text}:{port}"
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return PinnedTarget(
        scheme=parts.scheme,
        host=host,
        port=port,
        ip=str(chosen),
        path=path,
        pinned_base=f"{parts.scheme}://{netloc}",
        sni_hostname=host,
    )

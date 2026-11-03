"""Real TLS against a self-signed CA: certificate verification by hostname through a pinned IP."""

from __future__ import annotations

import asyncio
import datetime as dt
import socket
import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from vektra_assess.config import VektraAssessSettings
from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.result import Readable, UnreadableReason

TOKEN = "0123456789abcdef0123456789abcdef"


def _key():
    return ec.generate_private_key(ec.SECP256R1())


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_ca():
    key = _key()
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("test ca"))
        .issuer_name(_name("test ca"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def make_leaf(ca_key, ca_cert, dns: str):
    key = _key()
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(dns))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def write_pem(path: Path, *objs) -> None:
    out = b""
    for o in objs:
        if isinstance(o, x509.Certificate):
            out += o.public_bytes(serialization.Encoding.PEM)
        else:
            out += o.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
    path.write_bytes(out)


async def serve(certfile: Path):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(certfile))

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            await reader.readexactly(length)
            body = b'{"release": "5.2.4+"}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        except Exception:  # a failed handshake or a reset is the point of some tests
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    return server, server.sockets[0].getsockname()[1]


def client_for(ca_file: Path | None, lookups: list[str], answer: list[str]):
    env: dict[str, str] = {
        "VEKTRA_ASSESS_ENABLED": "true",
        "VEKTRA_ASSESS_MOODLE_ALLOWED_CIDRS": "127.0.0.0/8",
    }
    if ca_file is not None:
        env["VEKTRA_ASSESS_MOODLE_CA_FILE"] = str(ca_file)

    def resolver(host, port, *_a, **_k):
        lookups.append(host)
        ip = answer[0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    return MoodleWsClient(
        VektraAssessSettings(**env), token_provider=lambda _s: TOKEN, resolver=resolver
    )  # type: ignore[arg-type]


@pytest.fixture
def pki(tmp_path):
    ca_key, ca_cert = make_ca()
    write_pem(tmp_path / "ca.pem", ca_cert)
    good_key, good = make_leaf(ca_key, ca_cert, "moodle.test")
    write_pem(tmp_path / "good.pem", good, good_key)
    bad_key, bad = make_leaf(ca_key, ca_cert, "other.test")
    write_pem(tmp_path / "bad.pem", bad, bad_key)
    return tmp_path


async def test_certificate_is_verified_against_the_hostname_not_the_pinned_ip(pki):
    server, port = await serve(pki / "good.pem")
    lookups: list[str] = []
    client = client_for(pki / "ca.pem", lookups, ["127.0.0.1"])
    try:
        res = await client.call(
            SiteRef("s", f"https://moodle.test:{port}"), "core_webservice_get_site_info"
        )
        assert isinstance(res, Readable) and res.value == {"release": "5.2.4+"}
        assert lookups == [
            "moodle.test"
        ]  # one resolution, the connection went to 127.0.0.1
    finally:
        await client.aclose()
        server.close()


async def test_an_unknown_ca_is_a_tls_failure(pki):
    server, port = await serve(pki / "good.pem")
    client = client_for(
        None, [], ["127.0.0.1"]
    )  # system store: the test CA is not in it
    try:
        res = await client.call(SiteRef("s", f"https://moodle.test:{port}"), "f")
        assert (res.reason, res.detail_code) == (
            UnreadableReason.TLS,
            "tls_verify_failed",
        )  # type: ignore[union-attr]
    finally:
        await client.aclose()
        server.close()


async def test_a_certificate_for_another_name_is_refused_even_from_a_trusted_ca(pki):
    server, port = await serve(pki / "bad.pem")  # valid chain, wrong hostname
    client = client_for(pki / "ca.pem", [], ["127.0.0.1"])
    try:
        res = await client.call(SiteRef("s", f"https://moodle.test:{port}"), "f")
        assert res.reason is UnreadableReason.TLS  # type: ignore[union-attr]
    finally:
        await client.aclose()
        server.close()


async def test_rebinding_between_calls_is_caught_by_the_next_resolution(pki):
    server, port = await serve(pki / "good.pem")
    lookups: list[str] = []
    answer = ["127.0.0.1"]
    client = client_for(pki / "ca.pem", lookups, answer)
    site = SiteRef("s", f"https://moodle.test:{port}")
    try:
        assert isinstance(await client.call(site, "f"), Readable)
        answer[0] = "10.9.9.9"  # DNS now points outside the allowed range
        res = await client.call(site, "f")
        assert (res.reason, res.detail_code) == (
            UnreadableReason.ACCESS_EXCEPTION,
            "egress_denied",
        )  # type: ignore[union-attr]
        assert lookups == ["moodle.test", "moodle.test"]  # exactly one lookup per call
    finally:
        await client.aclose()
        server.close()

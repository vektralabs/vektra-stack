"""In-process TLS mail sink for the delivery tests (plan 10b): the ONLY accepted evidence of a send.

``aiosmtpd`` (dev dependency) on 127.0.0.1, in its own thread. It offers STARTTLS with a server
certificate issued by a throw-away CA generated per sink; the client trusts that CA through
``ca_file`` (the same ``VEKTRA_ASSESS_SMTP_CA_FILE`` path production uses for an on-prem relay).
Plaintext MAIL is refused (``require_starttls``) and AUTH needs TLS (``auth_require_tls``), so a
client that skipped TLS would fail here rather than pass.

Fault toggles (``MailSink.fault``): ``None`` (healthy), ``"4xx"`` / ``"5xx"`` (RCPT answered 451 /
550), ``"drop_after_data"`` (the message is STORED, then the connection is cut before the 250:
the crash-window shape). ``down()`` / ``up()`` stop and restart the listener on the same port
(connection refused in between). ``MailSink(starttls=False)`` advertises no STARTTLS at all.

False-pass guard: ``start()`` returns only after a real STARTTLS handshake against the listener
with the sink's own CA succeeded (a sink that is not listening, or not speaking TLS, fails the
fixture instead of making a delivery test vacuous).

Stored messages are queried by Message-ID: ``by_message_id(mid)`` returns every copy (a
crash-window duplicate shows up as two copies with the same id).
"""

from __future__ import annotations

import datetime as dt
import email
import ipaddress
import logging
import smtplib
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

HOST = "127.0.0.1"
USERNAME = "vektra-relay"
PASSWORD = "sink-password-not-a-secret"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


def make_ca_and_leaf(out: Path) -> tuple[Path, Path, Path]:
    """(ca.pem, leaf.pem, leaf.key) for 127.0.0.1 / localhost, valid for a day."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "vektra test mail CA")]
    )
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address(HOST)),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    out.mkdir(parents=True, exist_ok=True)
    ca_pem, leaf_pem, leaf_key = out / "ca.pem", out / "leaf.pem", out / "leaf.key"
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    leaf_pem.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    leaf_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_pem, leaf_pem, leaf_key


@dataclass
class Stored:
    rcpt: tuple[str, ...]
    raw: bytes
    received_at: float
    tls: bool
    authenticated: bool

    @property
    def message(self) -> EmailMessage:
        return email.message_from_bytes(self.raw, policy=policy.SMTP)  # type: ignore[return-value]

    @property
    def message_id(self) -> str | None:
        mid = self.message["Message-ID"]
        return str(mid) if mid is not None else None


@dataclass
class _Handler:
    sink: MailSink

    async def handle_RCPT(
        self, server: Any, session: Any, envelope: Any, address: str, options: list[str]
    ) -> str:
        fault = self.sink.fault
        if fault == "4xx":
            return "451 4.3.0 try again later"
        if fault == "5xx":
            return "550 5.1.1 no such user"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:
        self.sink._store(
            Stored(
                rcpt=tuple(envelope.rcpt_tos),
                raw=bytes(envelope.original_content or envelope.content),
                received_at=time.time(),
                tls=session.ssl is not None,
                authenticated=bool(session.authenticated),
            )
        )
        if self.sink.fault == "drop_after_data":
            # the message is held, the 250 never reaches the client
            server.transport.close()
        return "250 2.0.0 queued"


@dataclass
class MailSink:
    workdir: Path
    starttls: bool = True
    require_auth: bool = True
    fault: str | None = None
    port: int = field(default_factory=_free_port)
    messages: list[Stored] = field(default_factory=list)
    auth_attempts: list[bool] = field(
        default_factory=list
    )  # tls flag of each AUTH seen
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _controller: Controller | None = None

    def __post_init__(self) -> None:
        self.ca_file, self._leaf, self._key = make_ca_and_leaf(self.workdir)

    # --- lifecycle ---------------------------------------------------------------------

    def _server_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self._leaf, self._key)
        return ctx

    def _authenticator(
        self, server: Any, session: Any, envelope: Any, mechanism: str, data: Any
    ) -> AuthResult:
        self.auth_attempts.append(session.ssl is not None)
        ok = (
            isinstance(data, LoginPassword)
            and data.login == USERNAME.encode()
            and data.password == PASSWORD.encode()
        )
        # handled=False: aiosmtpd itself answers 535 on a refusal
        return AuthResult(success=ok, handled=False)

    def start(self) -> MailSink:
        # the relay's own protocol log (aiosmtpd ``mail.log``) echoes every RCPT and header: it
        # stands for an external server, not Vektra, and must not pollute the log canary checks
        relay_log = logging.getLogger("mail.log")
        relay_log.propagate = False
        relay_log.setLevel(logging.CRITICAL)
        kwargs: dict[str, Any] = {
            "hostname": HOST,
            "port": self.port,
            "authenticator": self._authenticator,
            "auth_require_tls": True,
            "auth_required": self.require_auth,
        }
        if self.starttls:
            kwargs |= {"tls_context": self._server_context(), "require_starttls": True}
        self._controller = Controller(_Handler(self), **kwargs)
        self._controller.start()
        self._guard()
        return self

    def stop(self) -> None:
        if self._controller is not None:
            self._controller.stop()
            self._controller = None

    def down(self) -> None:
        """The relay is unreachable (connection refused) until ``up()``."""
        self.stop()

    def up(self) -> None:
        self.start()

    def _guard(self) -> None:
        """False-pass guard: a listening socket, and (when offered) a successful STARTTLS
        handshake trusting only the sink's CA."""
        with socket.create_connection((HOST, self.port), timeout=5):
            pass
        if not self.starttls:
            return
        ctx = ssl.create_default_context(cafile=str(self.ca_file))
        with smtplib.SMTP(HOST, self.port, timeout=5) as client:
            client.ehlo()
            assert client.has_extn("starttls"), "mail sink does not offer STARTTLS"
            client.starttls(context=ctx)
            client.ehlo()
            assert client.sock is not None and isinstance(client.sock, ssl.SSLSocket)

    # --- evidence ----------------------------------------------------------------------

    def _store(self, item: Stored) -> None:
        with self._lock:
            self.messages.append(item)

    def by_message_id(self, mid: str) -> list[Stored]:
        with self._lock:
            return [m for m in self.messages if m.message_id == mid]

    def to(self, address: str) -> list[Stored]:
        with self._lock:
            return [m for m in self.messages if address in m.rcpt]

    def clear(self) -> None:
        with self._lock:
            self.messages.clear()
            self.auth_attempts.clear()


def sink_config(sink: MailSink, **over: Any) -> Any:
    """An ``SmtpConfig`` pointed at ``sink``, trusting only its CA."""
    from vektra_assess.delivery.smtp import SmtpConfig

    base: dict[str, Any] = {
        "host": HOST,
        "port": sink.port,
        "sender": "vektra@vektra.example.org",
        "tls_mode": "starttls",
        "username": USERNAME,
        "password": PASSWORD,
        "timeout_s": 5.0,
        "ca_file": sink.ca_file,
        "max_connections": 2,
    }
    base.update(over)
    return SmtpConfig(**base)

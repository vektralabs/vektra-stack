"""The SMTP client of the worker (REQ-075, INT-003): mandatory TLS, AUTH only after TLS.

Worker only: the API process never imports this module or ``aiosmtplib`` (import-linter
contract), and the SMTP credentials are a worker-only secret (``assess_smtp``, ARCH-071).

- ``tls_mode="starttls"`` (default, port 587): plain connect, EHLO, then STARTTLS is REQUIRED; a
  relay that does not offer it is a ``tls`` failure, never a plaintext send.
  ``tls_mode="implicit"`` (port 465): TLS from the first byte.
- Certificates are always validated (``ssl.create_default_context``, optional CA file for an
  on-prem relay with its own CA, TLS 1.2 minimum, hostname checked).
- AUTH runs only once the transport is encrypted (checked on the transport itself, not inferred
  from the mode), and only when credentials are configured.
- At most ``max_connections`` sessions at once (one semaphore per worker); every send and every
  probe has a hard timeout.
- Every failure is reduced to a fixed class (``ErrorClass``) and an SMTP code: exception text can
  carry the address or the relay's echo of it and is never kept or logged.

``send`` returns a ``SendResult``: ``ok`` only after the relay's final 250 to DATA (REQ-075: SENT
only on relay acceptance).
"""

from __future__ import annotations

import asyncio
import enum
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Literal

import aiosmtplib

TlsMode = Literal["starttls", "implicit"]


class ErrorClass(enum.StrEnum):
    TLS = "tls"  # no STARTTLS offered, handshake or certificate failure
    CONNECT = "connect"  # refused, reset, dropped, timed out
    TEMP_4XX = "temp_4xx"  # a 4xx reply: try again later
    PERM_5XX = "perm_5xx"  # a synchronous 5xx (recipient rejected, message refused)
    AUTH = "auth"  # credentials refused or AUTH not offered over TLS


#: classes that are retried with backoff; anything else is final (FAILED)
RETRYABLE = frozenset(
    {ErrorClass.TLS, ErrorClass.CONNECT, ErrorClass.TEMP_4XX, ErrorClass.AUTH}
)
#: classes that raise an operator alert on every occurrence (on top of every FAILED)
ALERTING = frozenset({ErrorClass.TLS, ErrorClass.AUTH})


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    sender: str
    tls_mode: TlsMode = "starttls"
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    timeout_s: float = 30.0
    ca_file: Path | None = None
    max_connections: int = 2

    def __post_init__(self) -> None:
        if self.max_connections < 1:
            raise ValueError("max_connections must be at least 1")
        if self.tls_mode not in ("starttls", "implicit"):
            raise ValueError("tls_mode must be 'starttls' or 'implicit'")


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error_class: ErrorClass | None = None
    smtp_code: int | None = None

    @property
    def retryable(self) -> bool:
        return self.error_class in RETRYABLE


QUIT_TIMEOUT_S = 5.0


class SlotBusy(Exception):
    """No relay session freed up within the bounded wait: nothing was attempted."""


def _tls_in_chain(exc: BaseException) -> bool:
    """aiosmtplib wraps a failed implicit-TLS handshake in ``SMTPConnectError``."""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, ssl.SSLError | ssl.CertificateError):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


class _Failure(Exception):
    """Internal: a classified failure (the original exception is dropped on purpose)."""

    def __init__(self, cls: ErrorClass, code: int | None = None) -> None:
        super().__init__(cls.value)
        self.cls = cls
        self.code = code


def _code_class(code: int | None, default: ErrorClass) -> ErrorClass:
    if code is not None and 400 <= code < 500:
        return ErrorClass.TEMP_4XX
    if code is not None and 500 <= code < 600:
        return ErrorClass.PERM_5XX
    return default


def tls_context(ca_file: Path | None = None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class SmtpSender:
    """One per worker process: the semaphore caps the concurrent relay sessions."""

    def __init__(self, config: SmtpConfig) -> None:
        self.config = config
        self._ctx = tls_context(config.ca_file)
        self._slots = asyncio.Semaphore(config.max_connections)
        #: sessions currently open (for the cap test); never more than max_connections
        self.open_sessions = 0
        self.peak_sessions = 0
        #: how long a send or the probe waits for a free session before giving up untried
        self.slot_wait_s = config.timeout_s
        #: QUIT runs after the outcome is known, under its own short bound
        self.quit_timeout_s = QUIT_TIMEOUT_S

    def worst_case_s(self) -> float:
        """Upper bound of one ``send`` or ``probe``: slot wait + session + QUIT."""
        return self.slot_wait_s + self.config.timeout_s + self.quit_timeout_s

    async def _open(self) -> aiosmtplib.SMTP:
        """A connected, encrypted (and, with an account, authenticated) session, or a
        classified ``_Failure``. Classes: ``connect`` for anything before the TLS handshake
        (refused, reset, a plaintext EHLO that fails); ``tls`` only for a missing STARTTLS, a
        failed handshake or a refused certificate; ``auth`` for the login."""
        cfg = self.config
        implicit = cfg.tls_mode == "implicit"
        client = aiosmtplib.SMTP(
            hostname=cfg.host,
            port=cfg.port,
            use_tls=implicit,
            start_tls=False,  # never opportunistic: STARTTLS is required and done below
            validate_certs=True,
            tls_context=self._ctx,
            timeout=cfg.timeout_s,
        )
        phase = "connect"
        try:
            await client.connect()  # implicit TLS: the handshake happens here
            if not implicit:
                await client.ehlo()
                if not client.supports_extension("starttls"):
                    raise _Failure(ErrorClass.TLS)
                phase = "tls"
                await client.starttls()
                phase = "connect"
                await client.ehlo()
            if client.get_transport_info("sslcontext") is None:
                raise _Failure(ErrorClass.TLS)  # never AUTH or MAIL in plaintext
            if cfg.username:
                phase = "auth"
                await client.login(cfg.username, cfg.password or "")
            return client
        except _Failure:
            client.close()
            raise
        except aiosmtplib.SMTPAuthenticationError as exc:
            client.close()
            raise _Failure(ErrorClass.AUTH, exc.code) from None
        except aiosmtplib.SMTPResponseException as exc:
            client.close()
            fallback = {"auth": ErrorClass.AUTH, "tls": ErrorClass.TLS}.get(
                phase, ErrorClass.CONNECT
            )
            raise _Failure(_code_class(exc.code, fallback), exc.code) from None
        except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
            client.close()
            if phase == "tls" or _tls_in_chain(exc):
                raise _Failure(ErrorClass.TLS) from None
            if phase == "auth":
                raise _Failure(ErrorClass.AUTH) from None
            raise _Failure(ErrorClass.CONNECT) from None

    async def _quit(self, client: aiosmtplib.SMTP) -> None:
        """Best effort, after the outcome is known: its own short bound, and nothing that
        happens here (an error, a timeout, a cancellation) changes the result."""
        try:
            async with asyncio.timeout(self.quit_timeout_s):
                await client.quit()
        except (Exception, asyncio.CancelledError):
            client.close()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """One of the ``max_connections`` relay sessions, waited for at most
        ``slot_wait_s``; ``SlotBusy`` when none frees up in time (nothing was attempted)."""
        try:
            async with asyncio.timeout(self.slot_wait_s):
                await self._slots.acquire()
        except TimeoutError:
            raise SlotBusy from None
        try:
            yield
        finally:
            self._slots.release()

    async def send(
        self, msg: EmailMessage, recipient: str, *, slot_held: bool = False
    ) -> SendResult:
        """One message to one recipient. ``ok`` only after the relay's final 250; ``ok``
        whatever happens to QUIT after it. ``slot_held``: the caller already holds a slot."""
        if not slot_held:
            async with self.slot():
                return await self.send(msg, recipient, slot_held=True)
        self.open_sessions += 1
        self.peak_sessions = max(self.peak_sessions, self.open_sessions)
        client: aiosmtplib.SMTP | None = None
        try:
            async with asyncio.timeout(self.config.timeout_s):
                client = await self._open()
                try:
                    await client.send_message(
                        msg, sender=self.config.sender, recipients=[recipient]
                    )
                except aiosmtplib.SMTPRecipientsRefused as exc:
                    code = exc.recipients[0].code if exc.recipients else None
                    return SendResult(
                        False, _code_class(code, ErrorClass.PERM_5XX), code
                    )
                except aiosmtplib.SMTPResponseException as exc:
                    cls = _code_class(exc.code, ErrorClass.CONNECT)
                    return SendResult(False, cls, exc.code)
                except ssl.SSLError:
                    return SendResult(False, ErrorClass.TLS)
                except (aiosmtplib.SMTPException, OSError):
                    # dropped mid-DATA: the relay may or may not hold the message, it never
                    # said 250, so this is not SENT (retried, same Message-ID)
                    return SendResult(False, ErrorClass.CONNECT)
            return SendResult(True, None, 250)
        except _Failure as f:
            return SendResult(False, f.cls, f.code)
        except TimeoutError:
            return SendResult(False, ErrorClass.CONNECT)
        finally:
            self.open_sessions -= 1
            if client is not None:
                await self._quit(client)

    async def probe(self) -> SendResult:
        """EHLO + STARTTLS + AUTH + QUIT: proves the path works and NEVER sends (INT-003).
        ``SlotBusy`` when no session frees up within ``slot_wait_s``."""
        async with self.slot():
            client: aiosmtplib.SMTP | None = None
            try:
                async with asyncio.timeout(self.config.timeout_s):
                    client = await self._open()
                return SendResult(True, None, None)
            except _Failure as f:
                return SendResult(False, f.cls, f.code)
            except TimeoutError:
                return SendResult(False, ErrorClass.CONNECT)
            finally:
                if client is not None:
                    await self._quit(client)

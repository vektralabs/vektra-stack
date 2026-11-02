"""Test-only forkserver preload: records what a sidecar child must never do.

Preloaded into the forkserver, so every child (a fork of it) carries the patches. Each
attempt appends one line to the file named by ``PARSER_GUARD_LOG``: an AF_INET/AF_INET6
connect, a name lookup, or a process spawn (subprocess, os.system, exec, posix_spawn).
The parsing child is itself a fork of the forkserver, which is not patched.
"""

from __future__ import annotations

import os
import socket
import subprocess

_LOG = os.environ.get("PARSER_GUARD_LOG")


def _note(kind: str) -> None:
    if _LOG:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{kind} pid={os.getpid()}\n")


def _install() -> None:
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(self: socket.socket, address: object) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _note("net-connect")
        real_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _note("net-connect")
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    real_gai = socket.getaddrinfo

    def getaddrinfo(*a: object, **k: object):  # type: ignore[no-untyped-def]
        _note("net-lookup")
        return real_gai(*a, **k)  # type: ignore[arg-type]

    real_popen = subprocess.Popen.__init__

    def popen(self, *a, **k):  # type: ignore[no-untyped-def]
        _note("spawn")
        real_popen(self, *a, **k)

    real_system = os.system

    def system(cmd: str) -> int:
        _note("spawn")
        return real_system(cmd)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    subprocess.Popen.__init__ = popen  # type: ignore[method-assign]
    os.system = system  # type: ignore[assignment]


def attempt_forbidden() -> None:
    """Control: what a hostile child might try; run in a child to prove the guard bites."""
    try:
        socket.create_connection(("127.0.0.1", 9), timeout=0.2)
    except OSError:
        pass
    try:
        subprocess.run(["true"], check=False)
    except OSError:
        pass


def hostile_child_main(in_conn, out_conn, err_w, spec):  # type: ignore[no-untyped-def]
    """A compromised child: answers the supervisor with hostile bytes (see spec ext_hint)."""
    in_conn.recv_bytes(1 << 20)
    flavour = spec["ext_hint"]
    if flavour == "list":
        out_conn.send_bytes(b"[1]\n")
    elif flavour == "deep":
        out_conn.send_bytes(b"[" * 200_000 + b"\n")
    elif flavour == "nostatus":
        out_conn.send_bytes(b'{"chars":0}\n')
    else:
        out_conn.send_bytes(b"\xff\xfe garbage")
    os._exit(0)


_install()

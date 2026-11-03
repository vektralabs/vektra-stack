"""Hermetic Moodle test support: replay the recorded WS responses through httpx.MockTransport.

The recordings (``tests/fixtures/moodle_ws/<branch>/``) come from the seeded dev envs
(``scripts/assess_record_ws.py``). A request with no recording is a test failure
(``Replay.unmatched``), never a silent pass.
"""

from __future__ import annotations

import copy
import json
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx

from vektra_assess.config import VektraAssessSettings
from vektra_assess.moodle.client import MoodleWsClient, SiteRef, flatten_params

FIXTURES = Path(__file__).parent / "fixtures" / "moodle_ws"
BRANCHES = ("4.3", "5.2")
TOKEN = "0123456789abcdef0123456789abcdef"
BASE_URL = "https://moodle.example.org"
_STD = {"wstoken", "wsfunction", "moodlewsrestformat"}


@dataclass(frozen=True)
class LiveEnv:
    """A seeded dev site the SAME reader tests can run against (``-m moodle``)."""

    url: str
    manifest: dict[str, Any]
    token: str


#: branch -> live env; empty in the hermetic suite. ``use_live`` fills it for a live test.
_LIVE: dict[str, LiveEnv] = {}


def use_live(branch: str, live: LiveEnv | None) -> None:
    if live is None:
        _LIVE.pop(branch, None)
    else:
        _LIVE[branch] = live


def now(branch: str) -> int:
    """The clock the manifest's close dates are judged against."""
    return int(time.time()) if branch in _LIVE else int(manifest(branch)["seeded_at"])


def manifest(branch: str) -> dict[str, Any]:
    if branch in _LIVE:
        return _LIVE[branch].manifest
    out: dict[str, Any] = json.loads((FIXTURES / branch / "manifest.json").read_text())
    return out


def uid(branch: str, name: str) -> int:
    return int(manifest(branch)["users"][name]["id"])


def site_for(branch: str) -> SiteRef:
    if branch in _LIVE:
        return SiteRef(
            f"live-{branch}", _LIVE[branch].url, release=manifest(branch)["release"]
        )
    return SiteRef("site-1", BASE_URL, release=manifest(branch)["release"])


def _key(wsfunction: str, params: dict[str, Any]) -> str:
    return json.dumps([wsfunction, sorted(flatten_params(params).items())])


class Replay:
    """Serves the recordings of one branch; ``mutate`` rewrites a response per function."""

    def __init__(self, branch: str) -> None:
        self.branch = branch
        self.live = branch in _LIVE
        self.responses: dict[str, Any] = {}
        self.by_name: dict[tuple[str, str], str] = {}
        for f in [] if self.live else sorted((FIXTURES / branch).glob("*__*.json")):
            doc = json.loads(f.read_text())
            key = _key(doc["wsfunction"], doc["params"])
            self.responses[key] = doc["response"]
            self.by_name[(doc["wsfunction"], doc["fixture"])] = key
        self.unmatched: list[str] = []
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.mutate: Callable[[str, Any], Any] | None = None
        self.files: dict[str, bytes] = {}
        self.file_requests: list[httpx.Request] = []

    def response(self, wsfunction: str, fixture: str) -> Any:
        return copy.deepcopy(self.responses[self.by_name[(wsfunction, fixture)]])

    def params(self, wsfunction: str, fixture: str) -> dict[str, Any]:
        key = self.by_name[(wsfunction, fixture)]
        for f in (FIXTURES / self.branch).glob(f"{wsfunction}__{fixture}.json"):
            params: dict[str, Any] = json.loads(f.read_text())["params"]
            assert _key(wsfunction, params) == key
            return params
        raise KeyError(fixture)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/webservice/pluginfile.php"):
            self.file_requests.append(request)
            data = self.files.get(request.url.path)
            if data is None:
                return httpx.Response(404, content=b"")
            return httpx.Response(200, content=data)
        form = dict(parse_qsl(request.content.decode()))
        self.calls.append(
            (form["wsfunction"], {k: v for k, v in form.items() if k not in _STD})
        )
        key = json.dumps(
            [
                form["wsfunction"],
                sorted((k, v) for k, v in form.items() if k not in _STD),
            ]
        )
        if key not in self.responses:
            self.unmatched.append(key)
            return httpx.Response(404, json={})
        body = copy.deepcopy(self.responses[key])
        if self.mutate is not None:
            body = self.mutate(form["wsfunction"], body)
        return httpx.Response(200, json=body)


def resolver(ip: str = "93.184.216.34") -> Callable[..., Any]:
    def fn(host: str, port: int, *_a: Any, **_k: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    return fn


class RecordingClient(MoodleWsClient):
    """Live client that records (wsfunction, params) so tests can assert what was called."""

    def __init__(
        self, *args: Any, calls: list[tuple[str, dict[str, str]]], **kw: Any
    ) -> None:
        super().__init__(*args, **kw)
        self._calls = calls

    async def call(self, site: SiteRef, wsfunction: str, params: Any = None) -> Any:
        self._calls.append(
            (wsfunction, {k: v for k, v in flatten_params(params or {}).items()})
        )
        return await super().call(site, wsfunction, params)


def make_client(replay: Replay, **env: str) -> MoodleWsClient:
    if replay.live:
        live = _LIVE[replay.branch]
        settings = VektraAssessSettings(  # type: ignore[arg-type]
            **{
                "VEKTRA_ASSESS_ENABLED": "true",
                "VEKTRA_ENV": "development",
                "VEKTRA_ASSESS_MOODLE_ALLOW_HTTP": "true",
                "VEKTRA_ASSESS_MOODLE_ALLOWED_CIDRS": "127.0.0.0/8,::1/128",
                "VEKTRA_ASSESS_WS_RPS": "1000",
                **env,
            }
        )
        # ``localhost`` resolves to ::1 first, where the dev containers do not listen, and
        # Moodle redirects any other Host to its wwwroot: pin the IPv4 loopback address
        return RecordingClient(
            settings,
            token_provider=lambda _s: live.token,
            calls=replay.calls,
            resolver=resolver("127.0.0.1"),
        )
    settings = VektraAssessSettings(  # type: ignore[arg-type]
        **{"VEKTRA_ASSESS_ENABLED": "true", "VEKTRA_ASSESS_WS_RPS": "1000", **env}
    )
    return MoodleWsClient(
        settings,
        token_provider=lambda _site: TOKEN,
        transport=httpx.MockTransport(replay.handler),
        resolver=resolver(),
    )

"""A fault-injecting OpenAI-compatible LLM endpoint for the chaos suite (plan 13a).

``POST /v1/chat/completions`` on 127.0.0.1, in-process, stdlib only. The fault mode can be
switched while it runs:

- ``ok``: a fixed completion at once (``stream: true`` gets SSE chunks and ``[DONE]``);
- ``latency``: the same reply after ``delay_s``;
- ``5xx``: ``503`` with an OpenAI-style error body;
- ``stall``: the request is read, then nothing is ever sent (the connection stays open): a provider
  that accepts and hangs, the case only a client timeout ends.

``requests`` counts what arrived by mode, so a scenario can prove the fault was actually hit
rather than passing because no call was made.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field

REPLY = "Risposta di prova del modello finto."


@dataclass
class LlmStub:
    mode: str = "ok"
    delay_s: float = 1.0
    port: int = 0
    requests: Counter[str] = field(default_factory=Counter)
    _server: asyncio.base_events.Server | None = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    async def start(self) -> LlmStub:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        """Close the listener and end every open connection, stalled ones included."""
        if self._server is not None:
            self._server.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:  # tracked so that stop() can end a stalled handler
            self._tasks.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.decode("latin-1").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1])
            body = json.loads(await reader.readexactly(length) or b"{}")
            mode = self.mode
            self.requests[mode] += 1
            if mode == "stall":
                await asyncio.sleep(3600)
                return
            if mode == "latency":
                await asyncio.sleep(self.delay_s)
            if mode == "5xx":
                payload = json.dumps(
                    {
                        "error": {
                            "message": "overloaded",
                            "type": "server_error",
                            "code": 503,
                        }
                    }
                ).encode()
                writer.write(_response(503, "application/json", payload))
            elif body.get("stream"):
                writer.write(_response(200, "text/event-stream", _sse(), chunked=False))
            else:
                writer.write(_response(200, "application/json", _completion()))
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            if task is not None:
                self._tasks.discard(task)


def _completion() -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "created": 0,
            "model": "stub",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": REPLY},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    ).encode()


def _sse() -> bytes:
    out = []
    for word in REPLY.split(" "):
        chunk = {
            "id": "chatcmpl-stub",
            "object": "chat.completion.chunk",
            "choices": [
                {"index": 0, "delta": {"content": word + " "}, "finish_reason": None}
            ],
        }
        out.append(f"data: {json.dumps(chunk)}\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode()


def _response(status: int, ctype: str, payload: bytes, chunked: bool = False) -> bytes:
    reason = {200: "OK", 503: "Service Unavailable"}[status]
    return (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
    ).encode() + payload

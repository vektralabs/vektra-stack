import { describe, expect, it } from "vitest";
import { SseParser, backoffMs, consume } from "../src/sse";

const STREAM =
  "event: state\ndata: {\"state\":\"ACTIVE\"}\n\n" +
  ": keep-alive\n\n" +
  "id: 1\r\nevent: reply_delta\r\ndata: {\"text\":\"Uno\"}\r\n\r\n" +
  "id: 2\revent: reply_delta\rdata: line one\rdata: line two\r\r" +
  "retry: 3000\nid: 3\nevent: closed\ndata: {}\n\n";

const EXPECTED = [
  { event: "state", data: "{\"state\":\"ACTIVE\"}" },
  { event: "reply_delta", data: "{\"text\":\"Uno\"}", id: "1" },
  { event: "reply_delta", data: "line one\nline two", id: "2" },
  { event: "closed", data: "{}", id: "3" },
];

function feed(chunks: string[]) {
  const p = new SseParser();
  return { events: chunks.flatMap((c) => p.push(c)), parser: p };
}

describe("SseParser", () => {
  it("parses LF, CRLF and CR streams, heartbeats and multi-line data", () => {
    const { events, parser } = feed([STREAM]);
    expect(events).toEqual(EXPECTED);
    expect(parser.lastEventId).toBe("3");
    expect(parser.retry).toBe(3000);
  });

  it("gives the same events on 1-byte chunks", () => {
    expect(feed(STREAM.split("")).events).toEqual(EXPECTED);
  });

  it("gives the same events on random chunk boundaries (fuzz)", () => {
    let seed = 42;
    const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
    for (let run = 0; run < 300; run++) {
      const chunks: string[] = [];
      let i = 0;
      while (i < STREAM.length) {
        const n = 1 + Math.floor(rnd() * 12);
        chunks.push(STREAM.slice(i, i + n));
        i += n;
      }
      expect(feed(chunks).events).toEqual(EXPECTED);
    }
  });

  it("keeps a CRLF split between two chunks as one line end", () => {
    expect(feed(["data: a\r", "\n\r", "\n"]).events).toEqual([{ event: "message", data: "a" }]);
  });
});

describe("consume", () => {
  const body = (s: string) =>
    new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(new TextEncoder().encode(s));
        c.close();
      },
    });

  it("sends the bearer header, never a query token, and resumes with Last-Event-ID", async () => {
    const calls: { url: string; headers: Record<string, string> }[] = [];
    const responses = [
      new Response(body("id: 7\nevent: reply_delta\ndata: x\n\n"), { status: 200 }),
      new Response(body("id: 8\nevent: closed\ndata: {}\n\n"), { status: 200 }),
    ];
    const seen: string[] = [];
    await consume({
      url: "/api/v1/assess/sessions/s/events",
      token: () => "TKN",
      onEvent: (e) => seen.push(e.event),
      onTerminal: () => seen.push("terminal"),
      fetchImpl: (async (url: string, init: RequestInit) => {
        calls.push({ url, headers: init.headers as Record<string, string> });
        return responses.shift()!;
      }) as unknown as typeof fetch,
      sleep: async () => {},
    });
    expect(seen).toEqual(["reply_delta", "closed"]);
    expect(calls[0].url).not.toContain("TKN");
    expect(calls[0].headers.Authorization).toBe("Bearer TKN");
    expect(calls[0].headers["Last-Event-ID"]).toBeUndefined();
    expect(calls[1].headers["Last-Event-ID"]).toBe("7");
  });

  it("stops on a terminal status without reconnecting", async () => {
    for (const status of [401, 403, 409]) {
      let n = 0;
      let terminal = 0;
      await consume({
        url: "/e",
        token: () => "T",
        onEvent: () => {},
        onTerminal: (s) => (terminal = s),
        fetchImpl: (async () => {
          n++;
          return new Response("", { status });
        }) as unknown as typeof fetch,
        sleep: async () => {},
      });
      expect([n, terminal]).toEqual([1, status]);
    }
  });

  it("backs off 1, 2, 4, 8 s (capped) with jitter", () => {
    expect([0, 1, 2, 3, 4, 9].map((a) => backoffMs(a, () => 0.5))).toEqual([
      1000, 2000, 4000, 8000, 8000, 8000,
    ]);
    expect(backoffMs(0, () => 0)).toBe(800);
    expect(backoffMs(0, () => 1)).toBe(1200);
  });
});

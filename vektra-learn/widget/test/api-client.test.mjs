// Widget token refresh on 401 (plan assess-foundations task 3.4).
//
// 0.8 binds learn tokens to aud/iss/kid, so a token minted before the upgrade is
// rejected once with 401. This proves the widget recovers by itself: it POSTs to
// data-token-refresh-url (block_vektra's ajax.php), takes the new token, and retries
// the request once with it. Runs the real ApiClient against a stubbed fetch.
import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiClient } from "../src/api-client.js";

function stubFetch(handler) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url, init });
    return handler(url, init, calls.length);
  };
  return calls;
}

const json = (status, body) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

test("401 with a pre-upgrade token: refresh, then retry with the new token", async () => {
  const calls = stubFetch((url, init) => {
    if (url === "/refresh") return json(200, { token: "fresh-token" });
    const auth = init.headers?.Authorization;
    return auth === "Bearer fresh-token"
      ? json(200, [{ id: "c1" }])
      : json(401, { detail: { code: "ERR-LEARN-003" } });
  });
  const client = new ApiClient("https://vektra.test", "legacy-token", "C1", {
    tokenRefreshUrl: "/refresh",
  });
  const turns = await client.getConversationTurns("c1");
  assert.deepEqual(turns, [{ id: "c1" }]);
  assert.equal(calls.length, 3);
  assert.equal(calls[0].init.headers.Authorization, "Bearer legacy-token");
  assert.equal(calls[1].url, "/refresh");
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[2].init.headers.Authorization, "Bearer fresh-token");
});

test("without a refresh URL the 401 surfaces (no silent success)", async () => {
  stubFetch(() => json(401, {}));
  const client = new ApiClient("https://vektra.test", "legacy-token", "C1");
  await assert.rejects(client.getConversationTurns("c1"), /HTTP 401/);
});

test("retry happens once: a second 401 is not looped", async () => {
  const calls = stubFetch((url) =>
    url === "/refresh" ? json(200, { token: "still-bad" }) : json(401, {}),
  );
  const client = new ApiClient("https://vektra.test", "legacy-token", "C1", {
    tokenRefreshUrl: "/refresh",
  });
  await assert.rejects(client.getConversationTurns("c1"), /HTTP 401/);
  assert.equal(calls.length, 3);
});

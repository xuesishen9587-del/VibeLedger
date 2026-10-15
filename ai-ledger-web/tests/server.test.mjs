import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createServer, publicConfig } from "../server.mjs";
const listen = (server) =>
  new Promise((resolve) =>
    server.listen(0, "127.0.0.1", () =>
      resolve(`http://127.0.0.1:${server.address().port}`),
    ),
  );
test("production static handler serves the favicon as SVG for GET and HEAD", async (t) => {
  const server = await createServer({
    dist: fileURLToPath(new URL("../public", import.meta.url)),
  });
  const base = await listen(server);
  t.after(() => {
    server.closeAllConnections();
    server.close();
  });
  const response = await fetch(base + "/favicon.svg");
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "image/svg+xml");
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.equal(
    await response.text(),
    await readFile(new URL("../public/favicon.svg", import.meta.url), "utf8"),
  );
  const head = await fetch(base + "/favicon.svg", { method: "HEAD" });
  assert.equal(head.status, 200);
  assert.equal(await head.text(), "");
  assert.match(
    await readFile(new URL("../index.html", import.meta.url), "utf8"),
    /rel="icon"[^>]+href="\/favicon.svg"/,
  );
});
test("runtime config exposes only public values", () => {
  assert.deepEqual(
    publicConfig({
      SUPABASE_URL: "https://test.supabase.co",
      SUPABASE_PUBLISHABLE_KEY: "service-role-secret",
      DATABASE_URL: "private",
    }),
    {
      supabaseUrl: "https://test.supabase.co",
      supabaseKey: "",
      timezone: "Asia/Singapore",
    },
  );
  assert.equal(
    publicConfig({
      SUPABASE_URL: "http://not-secure",
      SUPABASE_PUBLISHABLE_KEY: "sb_publishable_test",
    }).supabaseUrl,
    "",
  );
});
test("proxy preserves auth and idempotency but never forwards cookies; JSON stays uncacheable", async (t) => {
  let seen;
  const upstream = http.createServer(async (req, res) => {
    seen = req.headers;
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end('{"items":[]}');
  });
  const backend = await listen(upstream);
  const server = await createServer({ env: { BACKEND_URL: backend } });
  const base = await listen(server);
  t.after(() => {
    server.closeAllConnections();
    server.close();
    upstream.closeAllConnections();
    upstream.close();
  });
  const result = await fetch(base + "/api/v1/accounts", {
    headers: {
      Authorization: "Bearer test-token",
      Cookie: "private=cookie",
      "Idempotency-Key": "test-id",
    },
  });
  assert.equal(result.status, 200);
  assert.equal(result.headers.get("cache-control"), "no-store");
  assert.equal(seen.authorization, "Bearer test-token");
  assert.equal(seen["idempotency-key"], "test-id");
  assert.equal(seen.cookie, undefined);
  assert.deepEqual(await result.json(), { items: [] });
  assert.match(
    result.headers.get("content-security-policy"),
    /frame-ancestors 'none'/,
  );
});
test("redirects are not followed and non API routes cannot proxy arbitrary URLs", async (t) => {
  let hits = 0;
  const upstream = http.createServer((req, res) => {
    hits++;
    res.writeHead(302, { Location: "http://127.0.0.1:1/private" });
    res.end();
  });
  const backend = await listen(upstream);
  const server = await createServer({ env: { BACKEND_URL: backend } });
  const base = await listen(server);
  t.after(() => {
    server.closeAllConnections();
    server.close();
    upstream.closeAllConnections();
    upstream.close();
  });
  assert.equal((await fetch(base + "/api/v1/accounts")).status, 502);
  assert.equal((await fetch(base + "/api/v1/%2fprivate")).status, 400);
  assert.equal(hits, 1);
  assert.equal((await fetch(base + "/health")).status, 200);
});

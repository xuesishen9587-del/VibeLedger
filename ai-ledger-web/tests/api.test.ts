import { it, expect, vi, afterEach } from "vitest";
import { Api } from "../src/lib/api";
afterEach(() => vi.unstubAllGlobals());
it("keeps recovery when a successful response is truncated or not JSON", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response("truncated", { status: 200 })),
  );
  const api = new Api("alice", async () => "access");
  await expect(
    api.command("/transactions", "POST", { original_amount: "12.00" }),
  ).rejects.toThrow();
  expect(api.pending()).toHaveLength(1);
});
it("retries the exact original body and command key after network failure and page reload", async () => {
  const fetcher = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValueOnce(new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetcher);
  const api = new Api("alice", async () => "access");
  await expect(
    api.command("/transactions", "POST", { original_amount: "12.00" }),
  ).rejects.toThrow();
  const recovered = new Api("alice", async () => "fresh-access");
  const pending = recovered.pending()[0];
  expect(pending.body).toEqual({ original_amount: "12.00" });
  await recovered.retry(pending);
  expect(fetcher.mock.calls[0][1].headers.get("Idempotency-Key")).toBe(
    fetcher.mock.calls[1][1].headers.get("Idempotency-Key"),
  );
  expect(fetcher.mock.calls[1][1].body).toBe(fetcher.mock.calls[0][1].body);
  expect(recovered.pending()).toEqual([]);
});
it("isolates pending commands by user and clears them on explicit logout", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
  const a = new Api("alice", async () => "token");
  await expect(a.command("/accounts", "POST", {})).rejects.toThrow();
  expect(new Api("bob", async () => "token").pending()).toEqual([]);
  a.clear();
  expect(a.pending()).toEqual([]);
});
it("a deterministic validation failure is not offered as an uncertain retry", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "INVALID_AMOUNT" } }), {
        status: 422,
      }),
    ),
  );
  const a = new Api("alice", async () => "token");
  await expect(a.command("/transactions", "POST", {})).rejects.toThrow("金额");
  expect(a.pending()).toEqual([]);
});
it("a second write does not silently replace a pending command", async () => {
  const fetcher = vi.fn().mockRejectedValue(new Error("offline"));
  vi.stubGlobal("fetch", fetcher);
  const a = new Api("alice", async () => "token");
  await expect(
    a.command("/transactions", "POST", { amount: "1" }),
  ).rejects.toThrow();
  await expect(
    a.command("/transactions", "POST", { amount: "2" }),
  ).rejects.toThrow();
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(a.pending()[0].body).toEqual({ amount: "1" });
});
it("disposed identity never sends a late request using another session", async () => {
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  let resolve!: (value: string) => void;
  const token = new Promise<string>((r) => {
    resolve = r;
  });
  const a = new Api("alice", () => token);
  const request = a.request("/accounts");
  a.dispose();
  resolve("other-token");
  await expect(request).rejects.toThrow();
  expect(fetcher).not.toHaveBeenCalled();
});

import { ApiError } from "./errors";
export type Command = {
  key: string;
  path: string;
  method: string;
  body: unknown;
  label: string;
};
export class Api {
  private controller = new AbortController();
  constructor(
    public subject: string,
    private token: () => Promise<string | null>,
    private storage: Storage = sessionStorage,
  ) {}
  dispose() {
    this.controller.abort();
  }
  private storageKey() {
    return "vl-pending:" + this.subject;
  }
  pending(): Command[] {
    try {
      return JSON.parse(this.storage.getItem(this.storageKey()) || "[]");
    } catch {
      return [];
    }
  }
  private savePending(items: Command[]) {
    try {
      this.storage.setItem(this.storageKey(), JSON.stringify(items));
      window.dispatchEvent(new Event("vl-pending"));
    } catch {
      throw new ApiError("STORAGE_UNAVAILABLE");
    }
  }
  clear() {
    this.storage.removeItem(this.storageKey());
  }
  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const token = await this.token();
    if (this.controller.signal.aborted)
      throw new DOMException("Aborted", "AbortError");
    if (!token) {
      window.dispatchEvent(new Event("vl-unauthorized"));
      throw new ApiError("UNAUTHORIZED", 401);
    }
    const headers = new Headers(init.headers);
    headers.set("Authorization", "Bearer " + token);
    if (init.body && !(init.body instanceof FormData))
      headers.set("Content-Type", "application/json");
    let response: Response;
    try {
      response = await fetch("/api/v1" + path, {
        ...init,
        headers,
        cache: "no-store",
        signal: AbortSignal.any([
          this.controller.signal,
          AbortSignal.timeout(
            init.body instanceof FormData || path === "/balance-captures"
              ? 130000
              : 30000,
          ),
        ]),
      });
    } catch (e) {
      if (this.controller.signal.aborted) throw e;
      throw new ApiError("NETWORK");
    }
    let payload;
    try {
      payload = response.status === 204 ? {} : await response.json();
    } catch {
      // A truncated success response cannot prove that a write was applied.
      throw new ApiError("NETWORK");
    }
    if (this.controller.signal.aborted)
      throw new DOMException("Aborted", "AbortError");
    if (!response.ok) {
      const err = payload.error || payload.detail?.error || {};
      if (response.status === 401)
        window.dispatchEvent(new Event("vl-unauthorized"));
      throw new ApiError(
        err.code || "INVALID_REQUEST",
        response.status,
        err.details || {},
      );
    }
    return payload as T;
  }
  async command<T>(
    path: string,
    method: string,
    body: unknown,
    label = "保存记录",
  ): Promise<T> {
    const prior = this.pending().find(
      (c) => c.path === path && c.method === method,
    );
    if (prior) throw new ApiError("NETWORK");
    const command = { key: crypto.randomUUID(), path, method, body, label };
    this.savePending([...this.pending(), command]);
    return this.retry<T>(command);
  }
  async retry<T>(c: Command): Promise<T> {
    try {
      const result = await this.request<T>(c.path, {
        method: c.method,
        headers: { "Idempotency-Key": c.key },
        body: JSON.stringify(c.body),
      });
      this.forget(c.key);
      return result;
    } catch (e) {
      if (
        e instanceof ApiError &&
        e.status >= 400 &&
        e.status < 500 &&
        ![401, 408, 429].includes(e.status)
      )
        this.forget(c.key);
      throw e;
    }
  }
  forget(key: string) {
    this.savePending(this.pending().filter((c) => c.key !== key));
  }
}
export function query(
  values: Record<string, string | number | boolean | null | undefined>,
) {
  const p = new URLSearchParams();
  Object.entries(values).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== "") p.set(k, String(v));
  });
  return p.size ? "?" + p : "";
}

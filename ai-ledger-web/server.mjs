import http from "node:http";
import { readFile, stat } from "node:fs/promises";
import { resolve, extname, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
export function publicConfig(env) {
  const url = env.SUPABASE_URL || "";
  const key = env.SUPABASE_PUBLISHABLE_KEY || "";
  let valid = false;
  try {
    const parsed = new URL(url);
    valid =
      parsed.protocol === "https:" &&
      !parsed.username &&
      !parsed.password &&
      !parsed.search &&
      !parsed.hash &&
      parsed.pathname === "/";
  } catch {
    /* unconfigured */
  }
  return {
    supabaseUrl: valid ? url : "",
    supabaseKey: valid && key.startsWith("sb_publishable_") ? key : "",
    timezone: env.DASHBOARD_TIMEZONE || "Asia/Singapore",
  };
}
export async function createServer({
  env = process.env,
  development = false,
  dist = resolve(root, "dist"),
} = {}) {
  const config = publicConfig(env);
  const backend = new URL(env.BACKEND_URL || "http://127.0.0.1:8000");
  if (
    !["http:", "https:"].includes(backend.protocol) ||
    backend.username ||
    backend.password ||
    backend.search ||
    backend.hash ||
    backend.pathname !== "/"
  )
    throw new Error("BACKEND_URL must be an origin");
  const vite = development
    ? await (
        await import("vite")
      ).createServer({ root, server: { middlewareMode: true }, appType: "spa" })
    : null;
  const server = http.createServer(async (req, res) => {
    const send = (status, body) => {
      res.writeHead(status, {
        "Content-Type": "application/json; charset=utf-8",
      });
      res.end(JSON.stringify(body));
    };
    res.setHeader("Cache-Control", "no-store");
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("Referrer-Policy", "no-referrer");
    res.setHeader("X-Frame-Options", "DENY");
    if (!development)
      res.setHeader(
        "Content-Security-Policy",
        `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' ${config.supabaseUrl ? new URL(config.supabaseUrl).origin : ""}; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'`,
      );
    try {
      const url = new URL(req.url, "http://localhost");
      if (req.method === "GET" && url.pathname === "/health")
        return send(200, { status: "ok", service: "vibeledger-web" });
      if (req.method === "GET" && url.pathname === "/config")
        return send(200, config);
      if (url.pathname.startsWith("/api/")) {
        if (
          !/^\/api\/v1\/[A-Za-z0-9/_-]+$/.test(url.pathname) ||
          /(?:%2e|%2f|%5c|\.\.|\\)/i.test(req.url) ||
          !["GET", "POST", "PATCH", "PUT", "DELETE"].includes(req.method)
        )
          return send(400, { error: { code: "INVALID_REQUEST" } });
        const headers = {};
        for (const key of [
          "authorization",
          "content-type",
          "idempotency-key",
          "accept",
        ])
          if (req.headers[key]) headers[key] = req.headers[key];
        const chunks = [];
        let bytes = 0;
        for await (const chunk of req) {
          bytes += chunk.length;
          if (bytes > 22 * 1024 * 1024)
            return send(413, { error: { code: "STATEMENT_LIMIT_EXCEEDED" } });
          chunks.push(chunk);
        }
        const response = await fetch(
          new URL(url.pathname + url.search, backend),
          {
            method: req.method,
            headers,
            body: bytes ? Buffer.concat(chunks) : undefined,
            redirect: "manual",
            signal: AbortSignal.timeout(125000),
          },
        );
        if (response.status >= 300 && response.status < 400)
          return send(502, { error: { code: "BACKEND_UNAVAILABLE" } });
        res.writeHead(response.status, {
          "Content-Type":
            response.headers.get("content-type") || "application/json",
        });
        res.end(Buffer.from(await response.arrayBuffer()));
        return;
      }
      if (vite) return vite.middlewares(req, res);
      if (!["GET", "HEAD"].includes(req.method))
        return send(405, { error: { code: "INVALID_REQUEST" } });
      const pathname = decodeURIComponent(url.pathname);
      if (
        pathname.includes("..") ||
        pathname.includes("\\") ||
        pathname.includes("\0")
      )
        return send(400, { error: { code: "INVALID_REQUEST" } });
      let file = resolve(dist, "." + pathname);
      if (
        !file.startsWith(resolve(dist) + "/") &&
        !file.startsWith(resolve(dist) + "\\")
      )
        file = resolve(dist, "index.html");
      try {
        if (!(await stat(file)).isFile()) throw new Error();
      } catch {
        if (extname(pathname))
          return send(404, { error: { code: "NOT_FOUND" } });
        file = resolve(dist, "index.html");
      }
      const content = await readFile(file);
      const mime = {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
      };
      res.writeHead(200, {
        "Content-Type": mime[extname(file)] || "application/octet-stream",
      });
      res.end(req.method === "HEAD" ? undefined : content);
    } catch {
      if (!res.headersSent)
        send(502, { error: { code: "BACKEND_UNAVAILABLE" } });
      else res.end();
    }
  });
  server.on("close", () => {
    void vite?.close();
  });
  return server;
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  const server = await createServer({
    development: process.argv.includes("--dev"),
  });
  server.listen(
    Number(process.env.PORT || 3000),
    process.env.HOST || "127.0.0.1",
    () => process.stdout.write("VibeLedger web is ready.\n"),
  );
}

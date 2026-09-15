import { defineConfig } from "@playwright/test";
const python =
  process.env.VIBELEDGER_PYTHON ||
  (process.platform === "win32"
    ? "..\\venv_backend\\Scripts\\python.exe"
    : "python");
export default defineConfig({
  testDir: "tests/live",
  workers: 1,
  timeout: 45000,
  use: {
    baseURL: "http://127.0.0.1:3101",
    channel: process.env.CI ? undefined : "chrome",
    viewport: { width: 1366, height: 900 },
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: `${python} ../ai-ledger-backend/scripts/web_acceptance_server.py`,
      url: "http://127.0.0.1:8019/health",
      reuseExistingServer: false,
    },
    {
      command: "node server.mjs",
      url: "http://127.0.0.1:3101/health",
      env: {
        PORT: "3101",
        BACKEND_URL: "http://127.0.0.1:8019",
        SUPABASE_URL: "https://fixture.supabase.co",
        SUPABASE_PUBLISHABLE_KEY: "sb_publishable_fixture",
      },
      reuseExistingServer: false,
    },
  ],
  reporter: "list",
});

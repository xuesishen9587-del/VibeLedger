import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "tests/e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 30000,
  use: {
    baseURL: "http://127.0.0.1:3100",
    browserName: "chromium",
    channel: process.env.CI ? undefined : "chrome",
    viewport: { width: 1366, height: 900 },
    trace: "retain-on-failure",
  },
  webServer: {
    command: "node server.mjs",
    url: "http://127.0.0.1:3100/health",
    env: {
      PORT: "3100",
      SUPABASE_URL: "https://fixture.supabase.co",
      SUPABASE_PUBLISHABLE_KEY: "sb_publishable_fixture",
    },
    reuseExistingServer: false,
  },
  reporter: "list",
});

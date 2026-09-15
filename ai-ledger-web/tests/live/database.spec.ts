import { readFileSync } from "node:fs";
import { test, expect } from "@playwright/test";

test("browser → same-origin proxy → JWT-protected API → PostgreSQL writes are consistent", async ({
  page,
  request,
}) => {
  const fixture = JSON.parse(readFileSync("live-fixture.local.json", "utf-8"));
  const headers = { Authorization: "Bearer " + fixture.session.access_token };
  expect((await request.get("/api/v1/accounts")).status()).toBe(401);
  await page.route("https://fixture.supabase.co/**", (r) =>
    r.fulfill({
      status: 200,
      json: r.request().url().includes("/user")
        ? fixture.session.user
        : fixture.session,
    }),
  );
  const faults: string[] = [];
  page.on("pageerror", (e) => faults.push(e.message));
  await page.goto("/");
  await page.getByLabel("邮箱", { exact: true }).fill("test@example.com");
  await page.getByLabel("密码", { exact: true }).fill("fixture-only");
  await page.getByRole("button", { name: "进入我们的账本" }).click();
  await expect(
    page.getByRole("heading", { name: "我们的小日子，心里都有数。" }),
  ).toBeVisible();
  await page.goto("/#/spending/new");
  const editor = page.getByRole("dialog");
  await editor.getByLabel("金额", { exact: true }).fill("88.80");
  await editor
    .getByRole("combobox", { name: "分类", exact: true })
    .selectOption(fixture.category_id);
  await editor
    .getByRole("combobox", { name: "账户", exact: true })
    .selectOption(fixture.account_id);
  await editor
    .getByLabel("商户 / 用途", { exact: true })
    .fill("真实接口测试晚餐");
  await editor.getByRole("button", { name: "记好了", exact: true }).click();
  await expect(editor).toHaveCount(0);
  await page.goto("/#/wealth");
  await page.getByRole("button", { name: "更新余额", exact: true }).click();
  await page.getByRole("dialog").getByLabel("验收钱包 CNY").check();
  await page
    .getByRole("dialog")
    .getByLabel("当前余额（可填零）")
    .fill("1000.00");
  await page.getByRole("button", { name: "保存本次余额" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.goto("/#/statement");
  await page
    .getByLabel("PDF 账单", { exact: true })
    .setInputFiles({
      name: "statement.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("%PDF-browser-fixture"),
    });
  await page.getByRole("button", { name: "开始识别账单" }).click();
  await expect(page.getByTestId("statement-row")).toHaveCount(42);
  await expect(page.getByLabel("第 1 笔金额", { exact: true })).toHaveValue(
    "10.00",
  );
  await page.getByRole("button", { name: "确认导入整份账单" }).click();
  await expect(
    page.getByRole("heading", { name: "这份账单，记好了。" }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "这份账单，记好了。" }),
  ).toBeVisible();
  const records = await (
    await request.get("/api/v1/transactions?limit=100", { headers })
  ).json();
  expect(records.items).toHaveLength(42);
  const date = new Date().toISOString().slice(0, 10);
  const report = await (
    await request.get(`/api/v1/reports/spending?from=${date}&to=${date}`, {
      headers,
    })
  ).json();
  expect(report.net_spending).toBe("478.80");
  const wealth = await (
    await request.get("/api/v1/reports/wealth", { headers })
  ).json();
  expect(wealth.net_worth).toBe("1000.00");
  expect(faults).toEqual([]);
});

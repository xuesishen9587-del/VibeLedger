import { test, expect, type Page } from "@playwright/test";
const account = {
  id: "account",
  household_id: "home",
  name: "家庭信用卡",
  account_type: "credit",
  currency: "CNY",
  balance_scope: "信用卡总欠款",
  risk_level: null,
  status: "active",
  row_version: 1,
  statement_import_enabled: true,
  latest_snapshot: null,
  opened_on: "2020-01-01",
  owner_user_id: null,
};
const categories = [
  {
    id: "food",
    name: "餐饮美食",
    category_type: "expense",
    status: "active",
    row_version: 1,
    is_fallback: false,
  },
  {
    id: "shop",
    name: "日用购物",
    category_type: "expense",
    status: "active",
    row_version: 1,
    is_fallback: false,
  },
  {
    id: "travel",
    name: "交通出行",
    category_type: "expense",
    status: "active",
    row_version: 1,
    is_fallback: false,
  },
  {
    id: "other",
    name: "其他",
    category_type: "expense",
    status: "active",
    row_version: 1,
    is_fallback: true,
  },
];
function statement(count = 42, uncertain = false) {
  const lines = Array.from({ length: count }, (_, i) => ({
    row_id: "row-" + i,
    row_no: i + 1,
    action: i === 40 ? "skip" : "create",
    occurred_on: "2026-09-" + String((i % 14) + 1).padStart(2, "0"),
    original_amount:
      i === 40 ? "1800.00" : String([28, 168, 36, 42, 19.9, 65][i % 6]),
    original_currency: "CNY",
    transaction_type: i === 41 ? "refund" : "expense",
    kind: i === 40 ? "repayment" : i === 41 ? "refund" : "expense",
    category_id: ["food", "shop", "travel"][i % 3],
    category_uncertain: false,
    merchant: [
      "两个人的晚餐",
      "盒马鲜生",
      "地铁出行",
      "周末咖啡",
      "日常小采购",
      "附近的面包店",
    ][i % 6],
    remarks: i === 41 ? "账单退款" : null,
    reason: i === 40 ? "Not spending: repayment" : null,
    requires_review: uncertain && i === 0,
    duplicate_ids: [],
  }));
  return {
    request_id: "statement",
    status: "needs_confirmation",
    row_version: 3,
    draft: {
      account_id: "account",
      lines,
      balance: null,
      period_start: "2026-09-01",
      period_end: "2026-09-30",
      partial: false,
      identity_ok: true,
      acknowledge_partial: false,
      confirm_account_identity: false,
      warnings: uncertain
        ? [{ code: "STATEMENT_LINE_UNCERTAIN", row_id: "row-0" }]
        : [],
    },
    warnings: uncertain
      ? [{ code: "STATEMENT_LINE_UNCERTAIN", row_id: "row-0" }]
      : [],
  };
}
const totals = {
  gross_expenses: "6824.60",
  refunds: "128.00",
  net_spending: "6696.60",
  known_gross_expenses: "6824.60",
  known_refunds: "128.00",
  known_net_spending: "6696.60",
  missing_conversion_count: 0,
};
async function setup(
  page: Page,
  {
    count = 42,
    uncertain = false,
    shortExpiry = false,
    multipleAccounts = false,
    mixedReview = false,
    userEmail = "test@example.com",
    userId = "11111111-1111-4111-8111-111111111111",
  } = {},
) {
  const accounts = multipleAccounts
    ? [account, { ...account, id: "second-account", name: "备用信用卡" }]
    : [account];
  let receipt = statement(count, uncertain);
  if (mixedReview) {
    receipt.draft.lines[0].requires_review = true;
    // Existing receipts may expose only the first warning, hiding an invalid category.
    receipt.draft.lines[1].requires_review = true;
    receipt.draft.lines[1].category_id = "removed-category";
    receipt.draft.lines[2].transaction_type = "";
    receipt.warnings = receipt.draft.warnings = [
      { code: "STATEMENT_LINE_UNCERTAIN", row_id: "row-0" },
      { code: "STATEMENT_LINE_UNCERTAIN", row_id: "row-1" },
      { code: "INVALID_TRANSACTION_TYPE", row_id: "row-2" },
      { code: "POSSIBLE_DUPLICATE", row_id: "row-3" },
    ];
  }
  const writes: { path: string; body: any }[] = [];
  let refreshes = 0;
  const user = {
    id: userId,
    aud: "authenticated",
    email: userEmail,
    app_metadata: {},
    user_metadata: {},
    created_at: "2026-01-01T00:00:00Z",
  };
  const session = () => {
    const expires =
      Math.floor(Date.now() / 1000) +
      (shortExpiry && refreshes === 0 ? 100 : 3600);
    return {
      access_token:
        Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString(
          "base64url",
        ) +
        "." +
        Buffer.from(
          JSON.stringify({
            sub: user.id,
            exp: expires,
            iat: Math.floor(Date.now() / 1000),
            aud: "authenticated",
          }),
        ).toString("base64url") +
        ".test-signature",
      refresh_token: "test-refresh-token",
      expires_in: expires - Math.floor(Date.now() / 1000),
      expires_at: expires,
      token_type: "bearer",
      user,
    };
  };
  await page.route("**/config", (r) =>
    r.fulfill({
      json: {
        supabaseUrl: "https://fixture.supabase.co",
        supabaseKey: "sb_publishable_fixture",
        timezone: "Asia/Singapore",
      },
    }),
  );
  await page.route("https://fixture.supabase.co/**", async (r) => {
    const url = r.request().url();
    if (url.includes("grant_type=refresh_token")) refreshes++;
    await r.fulfill({
      status: 200,
      headers: { "access-control-allow-origin": "*" },
      json: url.includes("/user")
        ? user
        : url.includes("/logout")
          ? {}
          : session(),
    });
  });
  await page.route("**/api/v1/**", async (r) => {
    const url = new URL(r.request().url());
    const path = url.pathname.replace("/api/v1", "");
    const method = r.request().method();
    const body = method === "GET" ? null : r.request().postDataJSON();
    if (method !== "GET") writes.push({ path, body });
    let data: unknown = { items: [], next_cursor: null };
    if (path === "/accounts") data = { items: accounts, next_cursor: null };
    else if (path === "/categories")
      data = { items: categories, next_cursor: null };
    else if (path === "/reports/wealth")
      data = {
        as_of: "2026-09-15T00:00:00Z",
        reporting_currency: "CNY",
        setup_required: false,
        total_assets: "286542.80",
        total_liabilities: "8236.50",
        net_worth: "278306.30",
        known_assets: "286542.80",
        known_liabilities: "8236.50",
        known_net_worth: "278306.30",
        accounts: accounts.map((account) => ({
          account_id: account.id,
          name: account.name,
          account_type: "credit",
          currency: "CNY",
          balance: "-8236.50",
          converted_amount: "-8236.50",
          as_of: "2026-09-01T00:00:00Z",
          age_days: 14,
          needs_update: false,
          very_stale: false,
          balance_scope: account.balance_scope,
        })),
        risk_buckets: [
          { risk_level: "low", amount: "286542.80", percentage: "100" },
        ],
        coverage: {
          complete: true,
          missing_account_ids: [],
          stale_account_ids: [],
          missing_fx_currencies: [],
          stale_fx_currencies: [],
          oldest_observation_at: "2026-09-01",
          newest_observation_at: "2026-09-15",
        },
      };
    else if (path === "/reports/spending")
      data = {
        ...totals,
        reporting_currency: "CNY",
        from: "2026-09-01",
        to: "2026-09-15",
        schedules_current_through: "2026-09-15",
        category: categories.map((c, i) => ({
          ...totals,
          key: c.id,
          known_net_spending: String([2380, 1860, 930, 1526.6][i]),
        })),
        month: [],
      };
    else if (path === "/transactions")
      data = {
        items: receipt.draft.lines.slice(0, 5).map((l) => ({
          ...l,
          id: l.row_id,
          account_id: "account",
          row_version: 1,
          category_uncertain: false,
          account_review_acknowledged: false,
          status: "committed",
        })),
        next_cursor: null,
      };
    else if (path === "/review")
      data = {
        items: [],
        next_cursor: null,
        counts: {
          transactions: 0,
          drafts: receipt.status === "committed" ? 0 : 1,
          schedule_occurrences: 0,
          unusual_investment_estimates: 0,
        },
        settings_row_version: 1,
        investment_review_change_ratio: "0.20",
      };
    else if (path === "/reports/wealth-history") data = { points: [] };
    else if (path === "/reports/investments")
      data = { items: [], unavailable: [] };
    else if (path === "/ingestion-requests")
      data = {
        items: receipt.status === "needs_confirmation" ? [receipt] : [],
        next_cursor: null,
      };
    else if (path === "/ingestion-requests/statement/draft") {
      for (const edit of body.lines) {
        const line = receipt.draft.lines.find((l) => l.row_id === edit.row_id)!;
        Object.assign(line, edit);
        if (edit.confirm_facts) line.requires_review = false;
      }
      receipt.row_version++;
      receipt.warnings = receipt.draft.warnings = receipt.draft.lines.flatMap(
        (l) => {
          const code = !categories.some((c) => c.id === l.category_id)
            ? "INVALID_CATEGORY"
            : !["expense", "refund"].includes(l.transaction_type)
              ? "INVALID_TRANSACTION_TYPE"
              : l.requires_review
                ? "STATEMENT_LINE_UNCERTAIN"
                : mixedReview &&
                    l.row_id === "row-3" &&
                    !(l as any).confirm_facts
                  ? "POSSIBLE_DUPLICATE"
                  : null;
          return code ? [{ code, row_id: l.row_id }] : [];
        },
      );
      data = receipt;
    } else if (path === "/ingestion-requests/statement/confirm") {
      expect(body.expected_version).toBe(receipt.row_version);
      if (!receipt.warnings.length) receipt.status = "committed";
      data = {
        ...receipt,
        counts: { create: count - 1, link: 0, skip: 1 },
        snapshots: [],
      };
    } else if (path === "/ingestion-requests/statement") data = receipt;
    await r.fulfill({ status: 200, json: data });
  });
  return {
    writes,
    get refreshes() {
      return refreshes;
    },
  };
}
async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("邮箱", { exact: true }).fill("test@example.com");
  await page.getByLabel("密码", { exact: true }).fill("fixture-password");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("heading", { name: "总览" })).toBeVisible();
}
test("persistent login, desktop homepage and deep-link restore, logout clears session", async ({
  page,
}) => {
  await setup(page);
  await login(page);
  await expect(page.getByLabel("当前登录身份")).toContainText(
    "test@example.com",
  );
  await page.screenshot({
    path: "test-results/home-desktop.png",
    fullPage: true,
  });
  await page.reload();
  await expect(page.getByRole("heading", { name: "总览" })).toBeVisible();
  await page
    .getByRole("navigation", { name: "主要导航" })
    .getByRole("link", { name: "家庭财富" })
    .click();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "家庭财富", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
});
test("balance actions distinguish batch entry from each account after cards load", async ({
  page,
}) => {
  await setup(page, { multipleAccounts: true });
  await login(page);
  await page.goto("/#/wealth");
  const batch = page.getByRole("button", { name: "批量更新余额", exact: true });
  for (const name of ["家庭信用卡", "备用信用卡"]) {
    const single = page.getByRole("button", {
      name: `更新余额：${name}`,
      exact: true,
    });
    await expect(single).toBeVisible();
    await expect(batch).toHaveCount(1);
    await single.click();
    const dialog = page.getByRole("dialog", { name: "更新余额", exact: true });
    await expect(
      dialog.getByRole("checkbox", { name: `${name} CNY`, exact: true }),
    ).toBeChecked();
    await expect(dialog.getByRole("checkbox", { checked: true })).toHaveCount(
      1,
    );
    await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  }
  await batch.click();
  const dialog = page.getByRole("dialog", { name: "更新余额", exact: true });
  await expect(dialog.getByRole("checkbox", { checked: true })).toHaveCount(0);
  await dialog
    .getByRole("checkbox", { name: "家庭信用卡 CNY", exact: true })
    .check();
  await dialog
    .getByRole("checkbox", { name: "备用信用卡 CNY", exact: true })
    .check();
  await expect(dialog.getByRole("checkbox", { checked: true })).toHaveCount(2);
});

test("42-row statement shows at least 10 complete rows and imports with one confirmation", async ({
  page,
}) => {
  const state = await setup(page);
  await login(page);
  await page.goto("/#/statement/statement");
  await expect(page.getByTestId("statement-row")).toHaveCount(42);
  const visible = await page.getByTestId("statement-row").evaluateAll(
    (rows) =>
      rows.filter((el) => {
        const r = el.getBoundingClientRect();
        return r.top >= 0 && r.bottom <= innerHeight - 80;
      }).length,
  );
  await page.screenshot({
    path: "test-results/statement-desktop.png",
    fullPage: true,
  });
  expect(visible).toBeGreaterThanOrEqual(10);
  await page.getByRole("button", { name: "确认导入整份账单" }).click();
  await expect(page.getByRole("heading", { name: "账单已导入" })).toBeVisible();
  expect(state.writes.filter((w) => w.path.endsWith("/confirm"))).toHaveLength(
    1,
  );
  expect(state.writes.filter((w) => w.path.endsWith("/draft"))).toHaveLength(0);
});
test("100 rows retain inline edits across filters and reload; confirmation uses the saved version", async ({
  page,
}) => {
  const state = await setup(page, { count: 100, uncertain: true });
  await login(page);
  await page.goto("/#/statement/statement");
  await page.getByLabel("第 2 笔金额", { exact: true }).fill("88.80");
  await page.getByRole("button", { name: "只看待检查 1" }).click();
  await expect(page.getByTestId("statement-row")).toHaveCount(1);
  await page.getByRole("button", { name: "检查", exact: true }).click();
  await page.getByRole("button", { name: "这一笔已核对" }).click();
  await page.getByRole("button", { name: "全部 100 笔" }).click();
  await expect(page.getByLabel("第 2 笔金额", { exact: true })).toHaveValue(
    "88.80",
  );
  await page.reload();
  await expect(page.getByLabel("第 2 笔金额", { exact: true })).toHaveValue(
    "88.80",
  );
  await page.getByRole("button", { name: "确认导入整份账单" }).click();
  await expect(page.getByRole("heading", { name: "账单已导入" })).toBeVisible();
  const patch = state.writes.find((w) => w.path.endsWith("/draft"))!;
  expect(patch.body.lines[0].confirm_facts).toBe(true);
  expect(patch.body.lines[1].original_amount).toBe("88.80");
  expect(patch.body.lines[2].confirm_facts).toBeUndefined();
  expect(
    state.writes.find((w) => w.path.endsWith("/confirm"))!.body
      .expected_version,
  ).toBe(4);
});
test("phone layout keeps page width within the viewport and table scrolling inside its container", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page);
  await login(page);
  await page.screenshot({
    path: "test-results/home-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.goto("/#/statement/statement");
  await expect(page.getByTestId("statement-row")).toHaveCount(42);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: "test-results/statement-mobile.png",
    fullPage: true,
  });
});

test("an expiring login is renewed by the auth SDK without losing the open page", async ({
  page,
}) => {
  await page.clock.install();
  const state = await setup(page, { shortExpiry: true });
  await login(page);
  await page.goto("/#/wealth");
  await expect(
    page.getByRole("heading", { name: "家庭财富", exact: true }),
  ).toBeVisible();
  await page.clock.fastForward(90000);
  await expect.poll(() => state.refreshes).toBeGreaterThan(0);
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "家庭财富", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "登录" })).toHaveCount(0);
});

test("logging out clears open financial views and local draft data in the other tab", async ({
  page,
  context,
}) => {
  await setup(page);
  await login(page);
  const other = await context.newPage();
  await setup(other);
  await other.goto("/#/statement/statement");
  await expect(other.getByTestId("statement-row")).toHaveCount(42);
  await other.getByLabel("第 2 笔金额", { exact: true }).fill("99.90");
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(other.getByRole("heading", { name: "登录" })).toBeVisible();
  expect(
    await other.evaluate(() =>
      Object.keys(sessionStorage).filter((k) => k.startsWith("vl-")),
    ),
  ).toEqual([]);
  await other.reload();
  await expect(other.getByRole("heading", { name: "登录" })).toBeVisible();
});

test("a PDF password error returns to upload instead of opening a failed draft", async ({
  page,
}) => {
  await setup(page);
  await page.route("**/api/v1/accounts/account/statement-imports", (r) =>
    r.fulfill({
      status: 400,
      json: {
        error: {
          code: "STATEMENT_PASSWORD_INVALID",
          details: { request_id: "failed-password" },
        },
      },
    }),
  );
  await login(page);
  await page.goto("/#/statement");
  await page.getByLabel("PDF 账单", { exact: true }).setInputFiles({
    name: "locked.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-fixture"),
  });
  await page.getByLabel("PDF 密码（如有）").fill("wrong-password");
  await page.getByRole("button", { name: "开始识别账单" }).click();
  await expect(
    page.getByText("PDF 密码不正确，请重新填写。", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByLabel("PDF 密码（如有）")).toBeVisible();
  expect(page.url()).not.toContain("failed-password");
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).filter((k) => k.startsWith("vl-upload:")),
    ),
  ).toEqual([]);
});

test("mixed statement warnings resolve on review, expose real blockers, and import once", async ({
  page,
}) => {
  const state = await setup(page, { count: 5, mixedReview: true });
  await login(page);
  await page.goto("/#/statement/statement");
  await page.getByRole("button", { name: "只看待检查 4" }).click();
  await page
    .getByTestId("statement-row")
    .filter({ has: page.getByLabel("第 1 笔商户", { exact: true }) })
    .getByRole("button", { name: "检查", exact: true })
    .click();
  await page.getByRole("button", { name: "这一笔已核对", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "只看待检查 3" }),
  ).toBeVisible();
  await expect(page.getByLabel("第 1 笔商户", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "全部 5 笔" }).click();
  await expect(
    page
      .getByTestId("statement-row")
      .first()
      .getByRole("button", { name: "就绪" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "只看待检查 3" }).click();
  const invalid = page
    .getByTestId("statement-row")
    .filter({ has: page.getByLabel("第 2 笔商户", { exact: true }) });
  await expect(invalid).toContainText("请选择可用的支出分类");
  await invalid.getByRole("button", { name: "检查", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("核对不能代替修改");
  await dialog.getByRole("button", { name: "保存修改并重新检查" }).click();
  await expect(dialog).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "保存修改并重新检查" }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "只看待检查 3" }),
  ).toBeVisible();
  await expect(dialog).toContainText("请选择可用的支出分类");
  await dialog
    .getByRole("combobox", { name: "支出分类", exact: true })
    .selectOption("food");
  await dialog.getByRole("button", { name: "保存修改并重新检查" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "只看待检查 2" }),
  ).toBeVisible();
  await page
    .getByTestId("statement-row")
    .first()
    .getByRole("button", { name: "检查", exact: true })
    .click();
  await expect(dialog).toContainText("请选择交易性质：消费或退款");
  await dialog
    .getByRole("combobox", { name: "交易性质", exact: true })
    .selectOption("expense");
  await dialog.getByRole("button", { name: "保存修改并重新检查" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "只看待检查 1" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "检查", exact: true }).click();
  await expect(dialog).toContainText("可能已经记过");
  await dialog
    .getByRole("button", { name: "这一笔已核对", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "只看待检查 0" }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("button", { name: "只看待检查 0" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "确认导入整份账单" }).click();
  await expect(page.getByRole("heading", { name: "账单已导入" })).toBeVisible();
  expect(state.writes.filter((w) => w.path.endsWith("/confirm"))).toHaveLength(
    1,
  );
  expect(
    state.writes.find((w) => w.path.endsWith("/draft"))!.body.lines[1]
      .confirm_facts,
  ).toBeUndefined();
});

for (const uncertain of [false, true]) {
  test(`PDF processing appears immediately, reassures during a wait, and ${uncertain ? "preserves recovery after network failure" : "clears on receipt"}`, async ({
    page,
  }) => {
    await page.clock.install();
    await setup(page);
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let uploadKey: string | undefined;
    let uploads = 0;
    await page.route(
      "**/api/v1/accounts/account/statement-imports",
      async (r) => {
        uploads++;
        uploadKey = r.request().headers()["idempotency-key"];
        await gate;
        if (uncertain) await r.abort("failed");
        else await r.fulfill({ json: statement(5) });
      },
    );
    await page.route("**/api/v1/ingestion-requests/by-key/*", async (r) => {
      expect(r.request().url()).toContain(uploadKey);
      await r.fulfill({ json: statement(5) });
    });
    await login(page);
    await page.goto("/#/statement");
    await page.getByLabel("PDF 账单", { exact: true }).setInputFiles({
      name: "card.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("%PDF-fixture"),
    });
    await page.getByRole("button", { name: "开始识别账单" }).click();
    await expect(
      page.getByRole("progressbar", { name: "账单识别进行中" }),
    ).toBeVisible();
    await expect(
      page.getByText("正在读取并识别账单，请稍候", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "正在识别…" }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "开始识别账单" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "取消这次上传" }),
    ).toHaveCount(0);
    await page.clock.fastForward(31000);
    await expect(page.getByText(/识别仍在进行/)).toBeVisible();
    await expect(page.getByText("已等待 31 秒")).toBeVisible();
    release();
    if (uncertain) {
      await expect(
        page.getByRole("button", { name: "查询识别结果" }),
      ).toBeVisible();
      await expect(page.getByRole("progressbar")).toHaveCount(0);
      await page.reload();
      await page.getByRole("button", { name: "查询识别结果" }).click();
    }
    await expect(page.getByRole("heading", { name: "核对账单" })).toBeVisible();
    await expect(page.getByRole("progressbar")).toHaveCount(0);
    expect(uploads).toBe(1);
  });
}

test("current login identity changes with the signed-in account on mobile", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page);
  await login(page);
  await expect(page.getByLabel("当前登录身份")).toContainText(
    "test@example.com",
  );
  await page.goto("/#/settings");
  await page.getByRole("button", { name: "提醒与登录", exact: true }).click();
  await page
    .getByRole("button", { name: "退出登录", exact: true })
    .last()
    .click();
  await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
  await setup(page, {
    userEmail: "another-member@example.com",
    userId: "22222222-2222-4222-8222-222222222222",
  });
  await login(page);
  await expect(page.getByLabel("当前登录身份")).toContainText(
    "another-member@example.com",
  );
  await expect(page.getByLabel("当前登录身份")).not.toContainText(
    "test@example.com",
  );
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
});

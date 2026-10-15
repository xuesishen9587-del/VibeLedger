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
    ? [{ ...account }, { ...account, id: "second-account", name: "备用信用卡" }]
    : [{ ...account }];
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
    if (path === "/household-members")
      data = {
        items: [
          { user_id: "member-one", display_name: "小林", status: "active" },
          { user_id: "member-two", display_name: "小夏", status: "active" },
        ],
        next_cursor: null,
      };
    else if (path === "/accounts" && method === "POST") {
      const created = {
        ...account,
        ...body,
        id: "created-account",
        row_version: 0,
      };
      accounts.push(created);
      data = created;
    } else if (path.startsWith("/accounts/") && method === "PATCH") {
      const target = accounts.find((a) => path === `/accounts/${a.id}`)!;
      expect(body.expected_version).toBe(target.row_version);
      Object.assign(target, body, { row_version: target.row_version + 1 });
      data = target;
    } else if (path === "/accounts")
      data = { items: accounts, next_cursor: null };
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
    else if (path === "/spending-schedules/materialize")
      data = { schedules_current_through: "2026-09-20", occurrence_counts: {} };
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
for (const width of [1366, 390, 320]) {
  test(`wealth ownership scopes and long names fit a ${width}px viewport`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const fixture = await setup(page, { multipleAccounts: true });
    const name = "小林（家庭成员名称较长时仍可完整选择）";
    await page.route("**/api/v1/household-members", (route) =>
      route.fulfill({
        json: {
          items: [
            { user_id: "one", display_name: name, status: "active" },
            { user_id: "two", display_name: "小夏", status: "active" },
            { user_id: "empty", display_name: "原成员", status: "disabled" },
          ],
          next_cursor: null,
        },
      }),
    );
    await page.route(/\/api\/v1\/reports\/wealth(?:\?|$)/, async (route) => {
      const owner = new URL(route.request().url()).searchParams.get("owner");
      const empty = owner === "empty",
        missingFx = owner === "unassigned",
        all = !owner;
      const amount = owner === "two" ? "200.20" : all ? "300.30" : "100.10";
      const known = empty || missingFx ? "0.00" : amount,
        complete = !empty && !missingFx;
      const row = (id: string, title: string) => ({
        account_id: id,
        name: title,
        account_type: "savings",
        currency: missingFx ? "USD" : "CNY",
        balance: missingFx ? "30.00" : id === "account" ? "100.10" : "200.20",
        converted_amount: missingFx
          ? null
          : id === "account"
            ? "100.10"
            : "200.20",
        as_of: "2026-02-02T12:00:00+08:00",
        age_days: 1,
        needs_update: false,
        very_stale: false,
        balance_scope: title,
      });
      const rows = empty
        ? []
        : missingFx
          ? [row("account", "未分配外币账户")]
          : owner === "two"
            ? [row("second-account", "小夏的储蓄账户")]
            : all
              ? [
                  row("account", "小林的储蓄账户"),
                  row("second-account", "小夏的储蓄账户"),
                ]
              : [row("account", "小林的储蓄账户（名称较长的账户）")];
      await route.fulfill({
        json: {
          reporting_currency: "CNY",
          setup_required: false,
          accounts: rows,
          total_assets: complete ? amount : null,
          total_liabilities: complete ? "0.00" : null,
          net_worth: complete ? amount : null,
          known_assets: known,
          known_liabilities: "0.00",
          known_net_worth: known,
          risk_buckets: [
            {
              risk_level: "low",
              amount: known,
              percentage: complete ? "100.00" : null,
            },
          ],
          coverage: {
            complete,
            missing_account_ids: [],
            missing_fx_currencies: missingFx ? ["USD"] : [],
            stale_account_ids: [],
            stale_fx_currencies: [],
            oldest_observation_at: null,
            newest_observation_at: null,
          },
        },
      });
    });
    await login(page);
    await page.goto("/#/wealth");
    await expect(page.locator(".net-worth > span")).toContainText("家庭净资产");
    const owner = page.getByRole("combobox", { name: "财富账户归属" });
    await owner.selectOption("one");
    await expect(page.locator(".net-worth > span")).toContainText(
      name + "净资产",
    );
    await expect(page.locator(".account-card")).toHaveCount(1);
    await expect(page.locator(".section-heading .count")).toHaveText("1");
    await expect(page.getByText("小夏的储蓄账户", { exact: true })).toHaveCount(
      0,
    );
    await expect(
      page.getByRole("heading", { name: "家庭净资产变化", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "家庭投资收益", exact: true }),
    ).toBeVisible();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width);
    const bounds = await owner.boundingBox();
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    await page.screenshot({
      path: testInfo.outputPath(`wealth-${width}.png`),
      fullPage: true,
    });
    await owner.selectOption("two");
    await expect(page.locator(".net-worth > span")).toContainText("小夏净资产");
    await owner.selectOption("unassigned");
    await expect(page.locator(".net-worth > span")).toContainText("已知净资产");
    await expect(page.getByText(/部分账户余额或汇率尚未补齐/)).toBeVisible();
    await expect(page.getByText("100.00%", { exact: true })).toHaveCount(0);
    await owner.selectOption("empty");
    await expect(
      page.getByText("当前范围没有账户", { exact: true }),
    ).toBeVisible();
    await expect(page.locator(".wealth-summary")).toHaveCount(0);
    await expect(page.getByText(/部分账户余额或汇率尚未补齐/)).toHaveCount(0);
    await owner.selectOption("");
    await expect(page.locator(".net-worth > span")).toContainText("家庭净资产");
    expect(fixture.writes).toEqual([]);
  });
}

for (const width of [2560, 1366, 390, 320]) {
  test(`spending analysis reports and charts fit ${width}px without writing data`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: width === 2560 ? 1440 : 900 });
    const fixture = await setup(page);
    const current = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Singapore",
      year: "numeric",
      month: "2-digit",
    }).format(new Date());
    const previous = new Date(
      Date.UTC(Number(current.slice(0, 4)), Number(current.slice(5, 7)) - 2, 1),
    )
      .toISOString()
      .slice(0, 7);
    const amounts = [600, 300, 100, 80, 60, 40, 20];
    const labels = [
      "餐饮与日常食品",
      "家庭用品",
      "交通出行",
      "教育与订阅",
      "医疗健康",
      "礼物与社交",
      "其他消费",
    ];
    await page.route("**/api/v1/categories", (route) =>
      route.fulfill({
        json: {
          items: [
            ...categories,
            ...labels.map((name, i) => ({
              ...categories[0],
              id: "analysis-" + i,
              name,
            })),
          ],
          next_cursor: null,
        },
      }),
    );
    function amountsFor(gross: number, refunds: number) {
      return {
        gross_expenses: gross.toFixed(2),
        known_gross_expenses: gross.toFixed(2),
        refunds: refunds.toFixed(2),
        known_refunds: refunds.toFixed(2),
        net_spending: (gross - refunds).toFixed(2),
        known_net_spending: (gross - refunds).toFixed(2),
        missing_conversion_count: 0,
      };
    }
    let reportReads = 0,
      expanded = false;
    await page.route(/\/api\/v1\/reports\/spending(?:\?|$)/, (route) => {
      reportReads++;
      const url = new URL(route.request().url());
      const data = {
        ...totals,
        gross_expenses: null,
        known_gross_expenses: "300.00",
        refunds: null,
        known_refunds: "350.00",
        net_spending: null,
        known_net_spending: "-50.00",
        missing_conversion_count: 1,
        reporting_currency: "CNY",
        from: url.searchParams.get("from"),
        to: url.searchParams.get("to"),
        category: [
          {
            ...amountsFor(100, 0),
            key: "food",
            gross_expenses: null,
            refunds: null,
            net_spending: null,
            missing_conversion_count: 1,
          },
          { ...amountsFor(200, 350), key: "shop" },
        ],
        month: [
          {
            ...amountsFor(300, 350),
            key: previous,
            gross_expenses: null,
            refunds: null,
            net_spending: null,
            missing_conversion_count: 1,
          },
        ],
      };
      if (expanded) {
        Object.assign(data, amountsFor(1200, 450), {
          category: amounts.map((amount, i) => ({
            ...amountsFor(amount, i === 0 ? 450 : 0),
            key: "analysis-" + i,
          })),
          month: [
            {
              ...amountsFor(500, 0),
              key: new Date(
                Date.UTC(
                  Number(current.slice(0, 4)),
                  Number(current.slice(5, 7)) - 3,
                  1,
                ),
              )
                .toISOString()
                .slice(0, 7),
            },
            { ...amountsFor(300, 0), key: previous },
            { ...amountsFor(400, 450), key: current },
          ],
        });
      }
      return route.fulfill({ json: data });
    });
    await login(page);
    await page.goto("/#/spending");
    await page.getByRole("button", { name: "支出分析" }).click();
    await expect(
      page.getByRole("heading", { name: "月度净支出趋势" }),
    ).toBeVisible();
    await expect(page.locator(".analysis-trend-row")).toHaveCount(12);
    await expect(page.locator(".analysis-bar.refund-heavy")).toHaveCount(1);
    await expect(
      page.getByRole("heading", { name: "消费分类占比" }),
    ).toBeVisible();
    await expect(page.getByText("33.3%")).toBeVisible();
    await expect(page.getByText(/缺少换算汇率/)).toBeVisible();
    const overflowing = await page.evaluate(() => {
      const viewport = document.documentElement.clientWidth;
      return [...document.querySelectorAll<HTMLElement>("body *")]
        .filter(
          (el) =>
            el.getBoundingClientRect().width > 0 &&
            el.getBoundingClientRect().right > viewport + 1,
        )
        .map((el) => ({
          tag: el.tagName,
          className: String(el.className).slice(0, 90),
          right: Math.round(el.getBoundingClientRect().right),
          width: Math.round(el.getBoundingClientRect().width),
        }))
        .slice(0, 12);
    });
    expect(overflowing).toEqual([]);
    await page.screenshot({
      path: testInfo.outputPath(`spending-analysis-${width}.png`),
      fullPage: true,
    });
    const before = reportReads;
    expanded = true;
    await page
      .getByRole("combobox", { name: "分析时间范围" })
      .selectOption("3");
    await expect(page.locator(".analysis-trend-row")).toHaveCount(3);
    await expect(
      page.locator(".stats-three .stat").first().locator("strong"),
    ).toHaveText("¥750.00");
    await expect(page.locator(".analysis-category-list li")).toHaveCount(6);
    await expect(page.getByText("其余分类合计")).toBeVisible();
    expect(reportReads).toBe(before + 1);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width);
    if (width === 2560)
      await page.screenshot({
        path: testInfo.outputPath("spending-analysis-2k-complete.png"),
        fullPage: true,
      });
    // Materialization is an expected, idempotent system command on page entry.
    // The analytics view itself must not create or change any financial record.
    expect(
      fixture.writes.filter(
        (write) => write.path !== "/spending-schedules/materialize",
      ),
    ).toEqual([]);
  });
}

async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("邮箱", { exact: true }).fill("test@example.com");
  await page.getByLabel("密码", { exact: true }).fill("fixture-password");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("heading", { name: "总览" })).toBeVisible();
}
test("account ownership uses household names, persists, and can be explicitly cleared", async ({
  page,
}) => {
  const fixture = await setup(page);
  await login(page);
  await page.goto("/#/settings");
  const row = page
    .locator(".list-row")
    .filter({ has: page.getByText("家庭信用卡", { exact: true }) });
  await expect(
    row.getByText("归属：未设置归属", { exact: true }),
  ).toBeVisible();
  await row.getByRole("button", { name: "设置", exact: true }).click();
  const dialog = page.getByRole("dialog");
  const owner = dialog.getByRole("combobox", { name: /^账户归属/ });
  await expect(owner).toBeEnabled();
  await expect(owner).toHaveValue("");
  await owner.selectOption({ label: "小夏" });
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(row.getByText("归属：小夏", { exact: true })).toBeVisible();
  expect(fixture.writes).toEqual([
    {
      path: "/accounts/account",
      body: expect.objectContaining({
        owner_user_id: "member-two",
        expected_version: 1,
      }),
    },
  ]);
  await page.reload();
  await expect(row.getByText("归属：小夏", { exact: true })).toBeVisible();
  await row.getByRole("button", { name: "设置", exact: true }).click();
  await expect(owner).toHaveValue("member-two");
  await owner.selectOption("");
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(
    row.getByText("归属：未设置归属", { exact: true }),
  ).toBeVisible();
  expect(fixture.writes[1]).toEqual({
    path: "/accounts/account",
    body: expect.objectContaining({ owner_user_id: null, expected_version: 2 }),
  });
});

test("mobile account creation offers real members without assigning the logged-in user", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const fixture = await setup(page);
  await login(page);
  await page.goto("/#/settings");
  await page.getByRole("button", { name: "添加账户", exact: true }).click();
  const dialog = page.getByRole("dialog");
  const owner = dialog.getByRole("combobox", { name: /^账户归属/ });
  await expect(owner).toBeEnabled();
  await expect(owner).toHaveValue("");
  await dialog.getByLabel("账户名称", { exact: true }).fill("家庭储蓄");
  await dialog.getByLabel(/^这个余额包含什么/).fill("仅活期存款");
  await owner.selectOption({ label: "小林" });
  await page.screenshot({
    path: testInfo.outputPath("account-ownership.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true);
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  const row = page
    .locator(".list-row")
    .filter({ has: page.getByText("家庭储蓄", { exact: true }) });
  await expect(row.getByText("归属：小林", { exact: true })).toBeVisible();
  expect(fixture.writes).toEqual([
    {
      path: "/accounts",
      body: expect.objectContaining({
        name: "家庭储蓄",
        owner_user_id: "member-one",
      }),
    },
  ]);
});

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
test("balance editor offers read-only signed credit estimate and only fills after user click", async ({ page }) => {
  const fixture = await setup(page);
  await page.route("**/api/v1/reports/balance-estimates", (route) =>
    route.fulfill({ json: { as_of: "2026-10-15T12:00:00Z", items: [{
      account_id: "account", currency: "CNY", status: "ready",
      snapshot_id: "snapshot", baseline_date: "2026-10-01", baseline_balance: "-100.00",
      recorded_change: "-5.00", estimated_balance: "-105.00",
      transaction_count: 2, same_day_count: 0, currency_mismatch_count: 0,
    }] } }),
  );
  await login(page);
  await page.goto("/#/wealth");
  await page.getByRole("button", { name: /更新余额：家庭信用卡/ }).click();
  await expect(page.getByText(/推荐余额：/)).toContainText("105.00");
  const input = page.locator('input[name="account"]');
  await expect(input).toHaveValue("");
  await page.getByRole("button", { name: "使用推荐值" }).click();
  await expect(input).toHaveValue("105.00");
  await expect(page.locator('input[name="surplus-account"]')).not.toBeChecked();
  await input.fill("110.00");
  await expect(input).toHaveValue("110.00");
  expect(fixture.writes.filter((write) => write.path === "/balance-updates")).toEqual([]);
});

test("same-day ambiguous balance estimate never supplies a one-click value", async ({ page }) => {
  await setup(page);
  await page.route("**/api/v1/reports/balance-estimates", (route) =>
    route.fulfill({ json: { as_of: "2026-10-15T12:00:00Z", items: [{
      account_id: "account", currency: "CNY", status: "ambiguous_same_day",
      snapshot_id: "snapshot", baseline_date: "2026-10-01", baseline_balance: "-100.00",
      recorded_change: null, estimated_balance: null,
      transaction_count: 2, same_day_count: 1, currency_mismatch_count: 0,
    }] } }),
  );
  await login(page);
  await page.goto("/#/wealth");
  await page.getByRole("button", { name: /更新余额：家庭信用卡/ }).click();
  await expect(page.getByText(/无法确定先后顺序/)).toBeVisible();
  await expect(page.getByRole("button", { name: "使用推荐值" })).toHaveCount(0);
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

test("legacy global provider failure revalidates into actionable rows and imports once after correction", async ({
  page,
}) => {
  await setup(page, { count: 3 });
  const current: any = statement(3);
  current.warnings = current.draft.warnings = [
    {
      code: "IMPORT_CHANGED",
      message: "Provider evidence has conflicting financial facts.",
    },
  ];
  let patches = 0,
    confirmations = 0;
  await page.route(
    "**/api/v1/ingestion-requests/statement**",
    async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/draft")) {
        patches++;
        const body = route.request().postDataJSON();
        expect(body.expected_version).toBe(current.row_version);
        for (const edit of body.lines)
          Object.assign(
            current.draft.lines.find((r: any) => r.row_id === edit.row_id),
            edit,
          );
        current.warnings = current.draft.warnings =
          current.draft.lines[1].original_amount ===
          current.draft.lines[0].original_amount
            ? []
            : [0, 1].map((i) => ({
                code: "STATEMENT_PROVIDER_CONFLICT",
                row_id: `row-${i}`,
              }));
        current.row_version++;
      } else if (path.endsWith("/confirm")) {
        confirmations++;
        expect(current.warnings).toEqual([]);
        expect(route.request().postDataJSON().expected_version).toBe(
          current.row_version,
        );
        current.status = "committed";
        current.counts = { create: 2, link: 1, skip: 0 };
      }
      await route.fulfill({ json: current });
    },
  );
  await login(page);
  await page.goto("/#/statement/statement");
  const global = page.getByRole("alert", { name: "整单阻塞" });
  await expect(global).toBeVisible();
  await expect(
    global.getByRole("button", { name: "保存修改并重新检查整份账单" }),
  ).toBeEnabled();
  await expect(
    page.getByText("还有几处需要检查，已在列表中标出。"),
  ).toHaveCount(0);
  // The normal final-confirm action also revalidates legacy global warnings,
  // even if the user has made no new edits, instead of trapping the old receipt.
  await page.getByRole("button", { name: "确认导入整份账单" }).click();
  await expect(
    page.getByRole("button", { name: "只看待检查 2" }),
  ).toBeVisible();
  await expect(page.getByTestId("statement-row")).toHaveCount(2);
  await expect(global).toHaveCount(0);
  expect(patches).toBe(1);
  expect(confirmations).toBe(0);
  await expect(page.getByTestId("statement-row").first()).toContainText(
    "银行交易编号",
  );
  await page
    .getByTestId("statement-row")
    .nth(1)
    .getByRole("button", { name: "检查", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("仅点击已核对不能解除冲突");
  await dialog.getByRole("button", { name: "保存修改并重新检查" }).click();
  await expect(
    dialog.getByRole("button", { name: "保存修改并重新检查" }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "只看待检查 2" }),
  ).toBeVisible();
  await dialog.getByLabel("交易金额", { exact: true }).fill("28");
  await dialog.getByRole("button", { name: "保存修改并重新检查" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "只看待检查 0" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "确认导入整份账单" }).click();
  await expect(page.getByRole("heading", { name: "账单已导入" })).toBeVisible();
  expect(confirmations).toBe(1);
});

for (const source of ["draft", "confirm"]) {
  test(`a global ${source} blocker shows explicit recovery instead of directing users to zero review rows`, async ({
    page,
  }) => {
    await setup(page, { count: 3 });
    const current: any = statement(3);
    let failed = false,
      commits = 0;
    await page.route(
      "**/api/v1/ingestion-requests/statement**",
      async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (route.request().method() !== "GET") {
          const body = route.request().postDataJSON();
          expect(body.expected_version).toBe(current.row_version);
          if (path.endsWith("/draft") && body.period_start)
            current.draft.period_start = body.period_start;
          current.row_version++;
          if (!failed && path.endsWith(`/${source}`)) {
            failed = true;
            current.warnings = current.draft.warnings = [
              {
                code: "IMPORT_CHANGED",
                message: "Reload statement validation before saving.",
              },
            ];
          } else if (path.endsWith("/draft")) {
            current.warnings = current.draft.warnings = [];
          } else if (path.endsWith("/confirm")) {
            expect(current.warnings).toEqual([]);
            commits++;
            current.status = "committed";
          }
        }
        await route.fulfill({ json: current });
      },
    );
    await login(page);
    await page.goto("/#/statement/statement");
    if (source === "draft")
      await page.getByLabel("账单开始日期").fill("2026-08-31");
    await page.getByRole("button", { name: "确认导入整份账单" }).click();
    const global = page.getByRole("alert", { name: "整单阻塞" });
    await expect(global).toBeVisible();
    await expect(global).toContainText("未保存任何一笔交易");
    await expect(
      page.getByRole("button", { name: "只看待检查 0" }),
    ).toBeVisible();
    await expect(
      page.getByText(/已在列表中标出|请再看一下标出的记录/),
    ).toHaveCount(0);
    expect(commits).toBe(0);
    await global
      .getByRole("button", { name: "保存修改并重新检查整份账单" })
      .click();
    await expect(global).toHaveCount(0);
    if (source === "draft")
      await expect(page.getByLabel("账单开始日期")).toHaveValue("2026-08-31");
    expect(commits).toBe(0);
    await page.getByRole("button", { name: "确认导入整份账单" }).click();
    await expect(
      page.getByRole("heading", { name: "账单已导入" }),
    ).toBeVisible();
    expect(commits).toBe(1);
  });
}

test("legacy repeated PDF references revalidate and import every row without merchant edits", async ({
  page,
}) => {
  await setup(page, { count: 7 });
  const current: any = statement(7);
  const references = [
    "A-9MALQB8WWS7EAV",
    "A-9MALQB8WWS7EAV",
    "A-9M8VV2DGWQ6LAV",
    "A-9M8VV2DGWQ6LAV",
    "A-9LC54QKWWS94AV",
    "A-9LC54QKWWS94AV",
    "A-9LC54QKWWS94AV",
  ];
  const amounts = ["26.50", "10.00", "15.30", "2.00", "2.00", "20.00", "20.00"];
  current.draft.lines.forEach((line: any, i: number) =>
    Object.assign(line, {
      merchant: "Grab* " + references[i],
      original_amount: amounts[i],
      provider_transaction_id: references[i],
      original_currency: "SGD",
    }),
  );
  current.warnings = current.draft.warnings = current.draft.lines.map(
    (line: any) => ({
      code: "STATEMENT_PROVIDER_CONFLICT",
      row_id: line.row_id,
    }),
  );
  let patches = 0,
    confirmations = 0;
  await page.route(
    "**/api/v1/ingestion-requests/statement**",
    async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/draft")) {
        patches++;
        const body = route.request().postDataJSON();
        expect(body.lines.map((r: any) => r.row_id)).toEqual(
          current.draft.lines.map((r: any) => r.row_id),
        );
        expect(
          body.lines.every((r: any) => !r.merchant && !r.original_amount),
        ).toBe(true);
        current.draft.lines.forEach((line: any) =>
          Object.assign(line, {
            provider_reference: line.provider_transaction_id,
            provider_transaction_id: null,
          }),
        );
        current.warnings = current.draft.warnings = [];
        current.row_version++;
      } else if (path.endsWith("/confirm")) {
        confirmations++;
        expect(patches).toBe(1);
        expect(route.request().postDataJSON().expected_version).toBe(
          current.row_version,
        );
        expect(current.draft.lines.map((r: any) => r.merchant)).toEqual(
          references.map((r) => "Grab* " + r),
        );
        expect(current.draft.lines.map((r: any) => r.original_amount)).toEqual(
          amounts,
        );
        current.status = "committed";
        current.counts = { create: 7, link: 0, skip: 0 };
      }
      await route.fulfill({ json: current });
    },
  );
  await login(page);
  await page.goto("/#/statement/statement");
  await expect(
    page.getByRole("button", { name: "只看待检查 7" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "确认导入整份账单" }).click();
  await expect(page.getByRole("heading", { name: "账单已导入" })).toBeVisible();
  expect(confirmations).toBe(1);
});

test("schedule catch-up gates spending and exposes recoverable failure", async ({
  page,
}) => {
  await setup(page);
  await login(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const keys: string[] = [];
  let attempts = 0;
  let reportReads = 0;
  page.on("request", (r) => {
    if (new URL(r.url()).pathname === "/api/v1/reports/spending") reportReads++;
  });
  await page.route("**/api/v1/spending-schedules/materialize", async (r) => {
    keys.push(r.request().headers()["idempotency-key"]);
    attempts++;
    if (attempts === 1) {
      await gate;
      await r.fulfill({
        status: 503,
        json: { error: { code: "UNAVAILABLE" } },
      });
    } else
      await r.fulfill({ json: { schedules_current_through: "2026-09-20" } });
  });
  await page.getByRole("link", { name: "日常支出" }).click();
  await expect(page.getByText("正在补记到期月度支出，请稍候…")).toBeVisible();
  expect(reportReads).toBe(0);
  release();
  await expect(
    page.getByText("月度计划补记未完成，以下支出可能不完整。"),
  ).toBeVisible();
  await expect.poll(() => reportReads).toBe(1);
  await page.getByRole("button", { name: "重试补记" }).click();
  await expect(page.getByText("月度计划已检查至 2026-09-20")).toBeVisible();
  expect(keys[1]).toBe(keys[0]);
  expect(keys[2]).not.toBe(keys[0]);
  await expect(
    page.getByText("月度计划补记未完成，以下支出可能不完整。"),
  ).toHaveCount(0);
});

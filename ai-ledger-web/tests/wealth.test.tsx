import { render, screen, within, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { AppContext } from "../src/lib/context";
import { Api } from "../src/lib/api";
import { money } from "../src/lib/format";
import { WealthPage } from "../src/pages/Wealth";
import type {
  Account,
  HouseholdMember,
  Wealth,
  WealthAccount,
} from "../src/types";

const members: HouseholdMember[] = [
  { user_id: "one", display_name: "小林", status: "active" },
  { user_id: "two", display_name: "小夏", status: "active" },
  { user_id: "empty", display_name: "原成员", status: "disabled" },
];
const accounts: Account[] = ["日常钱包", "家庭储蓄", "外币投资"].map(
  (name, i) => ({
    id: String(i),
    household_id: "family",
    name,
    account_type: "savings",
    currency: i === 2 ? "USD" : "CNY",
    balance_scope: name,
    risk_level: "low",
    status: "active",
    row_version: 0,
    statement_import_enabled: false,
    latest_snapshot: null,
    opened_on: "2026-01-01",
    owner_user_id: ["one", "two", null][i],
  }),
);
const rows: WealthAccount[] = accounts.map((a, i) => ({
  account_id: a.id,
  name: a.name,
  account_type: a.account_type,
  currency: a.currency,
  balance: ["100.10", "200.20", "30.00"][i],
  converted_amount: ["100.10", "200.20", "213.70"][i],
  as_of: "2026-02-02T12:00:00+08:00",
  age_days: 1,
  needs_update: false,
  very_stale: false,
  balance_scope: a.balance_scope,
}));
function report(owner: string): Wealth {
  const selected = owner
    ? rows.filter((_, i) =>
        owner === "unassigned"
          ? accounts[i].owner_user_id === null
          : accounts[i].owner_user_id === owner,
      )
    : rows;
  const total =
    owner === "one"
      ? "100.10"
      : owner === "two"
        ? "200.20"
        : owner === "unassigned"
          ? "213.70"
          : "514.00";
  const empty = !selected.length;
  return {
    accounts: selected,
    reporting_currency: "CNY",
    setup_required: false,
    total_assets: empty ? null : total,
    total_liabilities: empty ? null : "0.00",
    net_worth: empty ? null : total,
    known_assets: empty ? "0.00" : total,
    known_liabilities: "0.00",
    known_net_worth: empty ? "0.00" : total,
    risk_buckets: [
      {
        risk_level: "low",
        amount: empty ? "0.00" : total,
        percentage: empty ? null : "100.00",
      },
    ],
    coverage: {
      complete: !empty,
      missing_account_ids: [],
      missing_fx_currencies: [],
      stale_account_ids: [],
      stale_fx_currencies: [],
      oldest_observation_at: null,
      newest_observation_at: null,
    },
  };
}
function setup(
  options: {
    memberFailures?: number;
    load?: (owner: string) => Wealth | Promise<Wealth>;
  } = {},
) {
  const api = new Api("fixture-user", async () => "fixture-token");
  let failures = options.memberFailures || 0;
  const request = vi.spyOn(api, "request").mockImplementation(async (path) => {
    if (path === "/household-members") {
      if (failures-- > 0) throw new Error("成员读取失败");
      return { items: members, next_cursor: null } as never;
    }
    if (path.startsWith("/reports/wealth-history"))
      return { points: [] } as never;
    if (path === "/reports/investments")
      return { items: [], unavailable: [] } as never;
    const owner =
      new URL(path, "http://fixture.invalid").searchParams.get("owner") || "";
    return (await (options.load || report)(owner)) as never;
  });
  const command = vi.spyOn(api, "command").mockResolvedValue({});
  const view = render(
    <AppContext.Provider
      value={{
        api,
        accounts,
        categories: [],
        timezone: "Asia/Singapore",
        revision: 0,
        refresh: vi.fn(),
        notify: vi.fn(),
      }}
    >
      <WealthPage />
    </AppContext.Provider>,
  );
  return { user: userEvent.setup(), request, command, ...view };
}

it("uses real member names and filters report accounts, totals and risk without changing shared family sections", async () => {
  const { user, request, command, container } = setup();
  await screen.findByText("家庭净资产");
  const select = screen.getByRole("combobox", { name: "财富账户归属" });
  expect(
    within(select).getByRole("option", { name: "原成员（已停用）" }),
  ).toBeEnabled();
  await user.selectOptions(select, "one");
  await screen.findByText("小林净资产");
  expect(request).toHaveBeenCalledWith("/reports/wealth?owner=one");
  expect(container.querySelector(".section-heading .count")).toHaveTextContent(
    "1",
  );
  expect(screen.queryByText("家庭储蓄")).not.toBeInTheDocument();
  expect(
    screen.queryByText("家庭净资产", { exact: true }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole("heading", { name: "小林 · 资产风险分布" }),
  ).toBeVisible();
  expect(screen.getByText("100.00%")).toBeVisible();
  expect(screen.getByRole("heading", { name: "家庭净资产变化" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "家庭投资收益" })).toBeVisible();
  expect(
    request.mock.calls.filter(([p]) => p.startsWith("/reports/wealth-history")),
  ).toHaveLength(1);
  expect(
    request.mock.calls.filter(([p]) => p === "/reports/investments"),
  ).toHaveLength(1);
  await user.selectOptions(select, "two");
  await screen.findByText("小夏净资产");
  expect(screen.getByText("家庭储蓄")).toBeVisible();
  await user.selectOptions(select, "unassigned");
  await screen.findByText("未分配账户净资产");
  expect(screen.getByText("外币投资")).toBeVisible();
  expect(screen.queryByText("家庭储蓄")).not.toBeInTheDocument();
  await user.selectOptions(select, "");
  await screen.findByText("家庭净资产");
  expect(container.querySelectorAll(".account-card")).toHaveLength(3);
  expect(command).not.toHaveBeenCalled();
});

it("shows an empty group without claiming missing balances, family setup or verified zero wealth", async () => {
  const { user, container } = setup();
  await screen.findByText("家庭净资产");
  await user.selectOptions(screen.getByRole("combobox"), "empty");
  await screen.findByText("当前范围没有账户");
  expect(screen.queryByText("添加第一个账户")).not.toBeInTheDocument();
  expect(
    screen.queryByText(/部分账户余额或汇率尚未补齐/),
  ).not.toBeInTheDocument();
  expect(container.querySelector(".wealth-summary")).toBeNull();
  expect(screen.queryByText("100.00%")).not.toBeInTheDocument();
});

it.each(["balance", "fx"])(
  "keeps %s gaps explicit and displays known subtotals rather than a complete total",
  async (missing) => {
    const { user, container } = setup({
      load: (owner) => {
        const result = report(owner);
        if (owner === "unassigned") {
          result.coverage.complete = false;
          result.total_assets =
            result.total_liabilities =
            result.net_worth =
              null;
          result.known_assets = result.known_net_worth = "0.00";
          result.accounts = [
            {
              ...result.accounts[0],
              balance: missing === "balance" ? null : "30.00",
              converted_amount: null,
            },
          ];
          result.coverage.missing_account_ids =
            missing === "balance" ? ["2"] : [];
          result.coverage.missing_fx_currencies =
            missing === "fx" ? ["USD"] : [];
          result.risk_buckets = [
            { risk_level: "low", amount: "0.00", percentage: null },
          ];
        }
        return result;
      },
    });
    await screen.findByText("家庭净资产");
    await user.selectOptions(screen.getByRole("combobox"), "unassigned");
    await screen.findByText("已知净资产");
    expect(screen.getByText("已知资产")).toBeVisible();
    expect(screen.getByText("已知负债")).toBeVisible();
    expect(screen.getByText(/部分账户余额或汇率尚未补齐/)).toBeVisible();
    expect(screen.queryByText("100.00%")).not.toBeInTheDocument();
    expect(container.querySelector(".account-value")).toHaveTextContent(
      missing === "balance" ? "—" : money("30.00", "USD"),
    );
  },
);

it("retries the failed member directory while keeping household and unassigned views available", async () => {
  const { user, request } = setup({ memberFailures: 1 });
  await screen.findByText("成员读取失败");
  await user.click(
    within(screen.getByRole("alert")).getByRole("button", { name: "重试" }),
  );
  await screen.findByRole("option", { name: "小夏" });
  expect(
    request.mock.calls.filter(([p]) => p === "/household-members"),
  ).toHaveLength(2);
});

it("does not replace a newly selected owner with a slower prior response", async () => {
  let resolveFirst!: (value: Wealth) => void;
  const first = new Promise<Wealth>((resolve) => {
    resolveFirst = resolve;
  });
  const { user } = setup({
    load: (owner) => (owner === "one" ? first : report(owner)),
  });
  await screen.findByText("家庭净资产");
  await user.selectOptions(screen.getByRole("combobox"), "one");
  await user.selectOptions(screen.getByRole("combobox"), "two");
  await screen.findByText("小夏净资产");
  await act(async () => {
    resolveFirst(report("one"));
    await first;
  });
  expect(screen.getByText("小夏净资产")).toBeVisible();
  expect(screen.queryByText("日常钱包")).not.toBeInTheDocument();
});

it("clears a failed selected report and retries that scope without showing the household totals", async () => {
  let fails = true;
  const { user, request } = setup({
    load: (owner) => {
      if (owner === "one" && fails) throw new Error("财富读取失败");
      return report(owner);
    },
  });
  await screen.findByText("家庭净资产");
  await user.selectOptions(screen.getByRole("combobox"), "one");
  await screen.findByText("财富读取失败");
  expect(
    screen.queryByText("家庭净资产", { exact: true }),
  ).not.toBeInTheDocument();
  fails = false;
  await user.click(
    within(screen.getByRole("alert")).getByRole("button", { name: "重试" }),
  );
  await screen.findByText("小林净资产");
  await waitFor(() =>
    expect(
      request.mock.calls.filter(([p]) => p === "/reports/wealth?owner=one"),
    ).toHaveLength(2),
  );
});

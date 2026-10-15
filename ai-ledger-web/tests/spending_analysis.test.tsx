import type { ReactNode } from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { AppContext } from "../src/lib/context";
import { Api } from "../src/lib/api";
import { money, monthRange, sumDecimal, today } from "../src/lib/format";
import { SpendingPage } from "../src/pages/Spending";
import {
  MonthlySpendingTrend,
  SpendingAnalysis,
  SpendingCategoryDonut,
  shiftMonth,
} from "../src/components/SpendingAnalysis";
import type { Spending, Totals } from "../src/types";

const totals: Totals = {
  gross_expenses: "300.00",
  refunds: "350.00",
  net_spending: "-50.00",
  known_gross_expenses: "300.00",
  known_refunds: "350.00",
  known_net_spending: "-50.00",
  missing_conversion_count: 0,
};
const categoryTotals = (key: string, amount: string, refunds = "0.00") => ({
  ...totals,
  key,
  gross_expenses: amount,
  known_gross_expenses: amount,
  refunds,
  known_refunds: refunds,
  net_spending: sumDecimal([amount, "-" + refunds])!,
  known_net_spending: sumDecimal([amount, "-" + refunds])!,
});
const monthTotals = (key: string, net: string, missing = 0) => ({
  ...totals,
  key,
  net_spending: missing ? null : net,
  known_net_spending: net,
  gross_expenses: missing ? null : net.startsWith("-") ? "0.00" : net,
  known_gross_expenses: net.startsWith("-") ? "0.00" : net,
  refunds: missing ? null : net.startsWith("-") ? net.slice(1) : "0.00",
  known_refunds: net.startsWith("-") ? net.slice(1) : "0.00",
  missing_conversion_count: missing,
});
function fixture(): Spending {
  const current = today("Asia/Singapore").slice(0, 7);
  return {
    ...totals,
    reporting_currency: "CNY",
    from: monthRange(shiftMonth(current, -11)).from,
    to: monthRange(current).to,
    schedules_current_through: null,
    category: [
      categoryTotals("a", "100.00"),
      categoryTotals("b", "200.00", "350.00"),
    ],
    month: [
      monthTotals(shiftMonth(current, -2), "150.00"),
      {
        ...monthTotals(shiftMonth(current, -1), "-200.00"),
        gross_expenses: "150.00",
        known_gross_expenses: "150.00",
        refunds: "350.00",
        known_refunds: "350.00",
      },
      monthTotals(current, "0.00"),
    ],
  };
}
const categories = ["a", "b"].map((id) => ({
  id,
  name: "分类" + id,
  category_type: "expense",
  status: "active",
  row_version: 0,
  is_fallback: false,
}));
function wrap(
  children: ReactNode,
  data = fixture(),
  load?: (path: string) => Promise<Spending>,
) {
  const api = new Api("spending-test-user", async () => "fixture-token");
  const request = vi.spyOn(api, "request").mockImplementation(async (path) => {
    if (!path.startsWith("/reports/spending"))
      return { items: [], next_cursor: null } as never;
    return (await (load ? load(path) : data)) as never;
  });
  const view = render(
    <AppContext.Provider
      value={{
        api,
        accounts: [],
        categories,
        timezone: "Asia/Singapore",
        revision: 0,
        refresh: vi.fn(),
        notify: vi.fn(),
      }}
    >
      {children}
    </AppContext.Provider>,
  );
  return { ...view, request };
}

it("shifts calendar months across year boundaries without local-time drift", () => {
  expect(shiftMonth("2026-01", -2)).toBe("2025-11");
  expect(shiftMonth("2025-11", 14)).toBe("2027-01");
});

it("keeps missing calendar months at zero and allows negative net spending", () => {
  const report = fixture();
  const end = today("Asia/Singapore").slice(0, 7);
  const { container } = wrap(
    <MonthlySpendingTrend
      report={report}
      fromMonth={shiftMonth(end, -11)}
      toMonth={end}
    />,
  );
  expect(container.querySelectorAll(".analysis-trend-row")).toHaveLength(12);
  expect(container.querySelectorAll(".analysis-bar.refund-heavy")).toHaveLength(
    1,
  );
  expect(
    container.querySelectorAll(".analysis-trend-row:not(:has(.analysis-bar))")
      .length,
  ).toBe(10);
});

it("uses gross expense categories for nonnegative donut shares without netting refunds or income", () => {
  const report = fixture();
  const { container } = wrap(<SpendingCategoryDonut report={report} />);
  expect(screen.getByText("33.3%")).toBeInTheDocument();
  expect(screen.getByText("66.7%")).toBeInTheDocument();
  expect(screen.getByText("分类a")).toBeInTheDocument();
  expect(screen.getByText("分类b")).toBeInTheDocument();
  expect(container.querySelector(".analysis-donut")).not.toBeNull();
});

it("scales sub-unit spending against the actual maximum instead of making it appear zero", () => {
  const report = fixture();
  report.month = [
    monthTotals("2026-01", "0.01"),
    monthTotals("2026-02", "-0.02"),
  ];
  const { container } = wrap(
    <MonthlySpendingTrend
      report={report}
      fromMonth="2026-01"
      toMonth="2026-02"
    />,
  );
  const bars = container.querySelectorAll<HTMLElement>(".analysis-bar");
  expect(bars[0].style.width).toBe("24.5%");
  expect(bars[1].style.width).toBe("49%");
});

it("groups extra expense categories precisely and explains incomplete FX amounts", () => {
  const report = fixture();
  report.category = Array.from({ length: 7 }, (_, i) =>
    categoryTotals(String(i), "0.01"),
  );
  report.known_gross_expenses = "0.07";
  report.gross_expenses = null;
  report.missing_conversion_count = 1;
  const { container } = wrap(<SpendingCategoryDonut report={report} />, report);
  expect(screen.getByText("其余分类合计")).toBeInTheDocument();
  expect(screen.getByText("28.6%")).toBeInTheDocument();
  expect(screen.getByText(/仅以已换算金额计算占比/)).toBeInTheDocument();
  expect(container.querySelectorAll(".analysis-category-list li")).toHaveLength(
    6,
  );
});

it("requests one report for the selected range and rejects invalid custom dates", async () => {
  const current = today("Asia/Singapore").slice(0, 7);
  const user = userEvent.setup();
  const { request } = wrap(<SpendingAnalysis />);
  await screen.findByRole("heading", { name: "月度净支出趋势" });
  expect(request).toHaveBeenCalledWith(
    "/reports/spending?from=" +
      monthRange(shiftMonth(current, -11)).from +
      "&to=" +
      today("Asia/Singapore"),
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "分析时间范围" }),
    "3",
  );
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/reports/spending?from=" +
        monthRange(shiftMonth(current, -2)).from +
        "&to=" +
        today("Asia/Singapore"),
    ),
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "分析时间范围" }),
    "custom",
  );
  fireEvent.change(screen.getByLabelText("分析开始月份"), {
    target: { value: current },
  });
  fireEvent.change(screen.getByLabelText("分析结束月份"), {
    target: { value: shiftMonth(current, -2) },
  });
  expect(await screen.findByText(/请选择有效的起止月份/)).toBeInTheDocument();
});

it("labels absent months separately from recorded zero and incomplete known zero", () => {
  const report = fixture();
  const current = today("Asia/Singapore").slice(0, 7);
  report.month = [
    monthTotals(shiftMonth(current, -1), "0.00"),
    monthTotals(current, "0.00", 1),
  ];
  wrap(
    <MonthlySpendingTrend
      report={report}
      fromMonth={shiftMonth(current, -2)}
      toMonth={current}
    />,
  );
  const rows = screen.getAllByRole("listitem");
  expect(rows[0]).toHaveAccessibleName(/无记录，不代表实际没有消费/);
  expect(within(rows[0]).getByText("无记录")).toBeVisible();
  expect(rows[1]).toHaveAccessibleName(/净支出.*0.00/);
  expect(rows[2]).toHaveAccessibleName(/已知净支出.*部分交易缺汇率/);
});

it("labels all missing-FX totals as known and does not draw a zero-valued donut", async () => {
  const report = fixture();
  report.gross_expenses = report.refunds = report.net_spending = null;
  report.known_gross_expenses =
    report.known_refunds =
    report.known_net_spending =
      "0.00";
  report.missing_conversion_count = 2;
  report.category = [];
  const current = today("Asia/Singapore").slice(0, 7);
  report.month = [monthTotals(current, "0.00", 2)];
  const { container } = wrap(<SpendingAnalysis />, report);
  await screen.findByText("已知退款");
  expect(screen.getByText("已知净支出")).toBeVisible();
  expect(screen.getByText("已知消费")).toBeVisible();
  expect(screen.getByText(/当前仅显示已知小计/)).toBeVisible();
  expect(container.querySelector(".analysis-donut")).toBeNull();
  expect(screen.getByText(/目前无法绘制消费占比/)).toBeVisible();
  expect(screen.queryByText("100.0%")).not.toBeInTheDocument();
});

it.each(["0.00", "125.50"])(
  "keeps zero-consumption and refund-only periods truthful (refund %s)",
  async (refunds) => {
    const report = fixture();
    report.gross_expenses = report.known_gross_expenses = "0.00";
    report.refunds = report.known_refunds = refunds;
    report.net_spending = report.known_net_spending =
      refunds === "0.00" ? "0.00" : "-125.50";
    report.category = [];
    report.month = [];
    const { container } = wrap(<SpendingAnalysis />, report);
    await screen.findByText("暂无可统计的消费金额");
    const stats = container.querySelectorAll(".stat");
    expect(stats[0]).toHaveTextContent(money(report.net_spending));
    expect(stats[2]).toHaveTextContent(money(refunds));
    expect(container.querySelector(".analysis-donut")).toBeNull();
  },
);

it("hides previous totals immediately on range changes and ignores slower responses", async () => {
  let resolveFirst!: (data: Spending) => void;
  const pending = new Promise<Spending>((resolve) => {
    resolveFirst = resolve;
  });
  const current = today("Asia/Singapore").slice(0, 7);
  const firstStart = shiftMonth(current, -2) + "-01";
  const data = fixture();
  const { request, container } = wrap(
    <SpendingAnalysis />,
    data,
    async (path) => {
      if (path.includes("from=" + firstStart)) return pending;
      return data;
    },
  );
  await screen.findByText("消费合计");
  const user = userEvent.setup();
  await user.selectOptions(screen.getByRole("combobox"), "3");
  expect(container.querySelector(".stats-three")).toBeNull();
  expect(screen.getByRole("status")).toBeVisible();
  await user.selectOptions(screen.getByRole("combobox"), "6");
  await screen.findByText("消费合计");
  const stale = { ...fixture(), net_spending: "9999.00" };
  await act(async () => {
    resolveFirst(stale);
    await pending;
  });
  expect(screen.queryByText(money("9999.00"))).not.toBeInTheDocument();
  expect(request).toHaveBeenCalledTimes(3);
});

it("retries the selected range without falling back to an older successful report", async () => {
  let fails = false;
  const { request, container } = wrap(
    <SpendingAnalysis />,
    fixture(),
    async () => {
      if (fails) throw new Error("分析暂时无法读取");
      return fixture();
    },
  );
  await screen.findByText("消费合计");
  const user = userEvent.setup();
  fails = true;
  await user.selectOptions(screen.getByRole("combobox"), "6");
  await screen.findByText("分析暂时无法读取");
  expect(container.querySelector(".stats-three")).toBeNull();
  fails = false;
  await user.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByText("消费合计");
  expect(request.mock.calls.at(-1)?.[0]).toEqual(
    request.mock.calls.at(-2)?.[0],
  );
});

it("accepts historical custom months, blocks future or oversized ranges, and caps this month at the household date", async () => {
  const current = today("Asia/Singapore").slice(0, 7);
  const user = userEvent.setup();
  const { request } = wrap(<SpendingAnalysis />);
  await screen.findByText("消费合计");
  await user.selectOptions(screen.getByRole("combobox"), "custom");
  expect(request).toHaveBeenCalledTimes(1); // Same 12-month path is reused.
  fireEvent.change(screen.getByLabelText("分析结束月份"), {
    target: { value: shiftMonth(current, -1) },
  });
  await waitFor(() =>
    expect(request.mock.calls.at(-1)?.[0]).toContain(
      "to=" + monthRange(shiftMonth(current, -1)).to,
    ),
  );
  const before = request.mock.calls.length;
  fireEvent.change(screen.getByLabelText("分析结束月份"), {
    target: { value: shiftMonth(current, 1) },
  });
  await screen.findByRole("alert");
  expect(request).toHaveBeenCalledTimes(before);
  fireEvent.change(screen.getByLabelText("分析开始月份"), {
    target: { value: shiftMonth(current, -24) },
  });
  fireEvent.change(screen.getByLabelText("分析结束月份"), {
    target: { value: current },
  });
  expect(request).toHaveBeenCalledTimes(before);
});

it("loads one analysis report per range and pauses hidden transaction and plan queries", async () => {
  const { request } = wrap(<SpendingPage />);
  const user = userEvent.setup();
  await screen.findByRole("button", { name: "支出分析" });
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      expect.stringContaining("/transactions?"),
    ),
  );
  await user.click(screen.getByRole("button", { name: "支出分析" }));
  await screen.findByRole("heading", { name: "月度净支出趋势" });
  request.mockClear();
  await user.selectOptions(
    screen.getByRole("combobox", { name: "分析时间范围" }),
    "3",
  );
  await screen.findByRole("heading", { name: "月度净支出趋势" });
  expect(request).toHaveBeenCalledTimes(1);
  expect(request.mock.calls[0][0]).toMatch(/^\/reports\/spending\?/);
  await user.click(screen.getByRole("button", { name: "支出记录" }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      expect.stringContaining("/transactions?"),
    ),
  );
});

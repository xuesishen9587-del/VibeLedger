import type { ReactNode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { AppContext } from "../src/lib/context";
import { Api } from "../src/lib/api";
import { monthRange, today } from "../src/lib/format";
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
const categoryTotals = (key: string, amount: string) => ({
  ...totals,
  key,
  gross_expenses: amount,
  known_gross_expenses: amount,
});
const monthTotals = (key: string, net: string, missing = 0) => ({
  ...totals,
  key,
  net_spending: missing ? null : net,
  known_net_spending: net,
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
      categoryTotals("b", "200.00"),
    ],
    month: [
      monthTotals(shiftMonth(current, -2), "150.00"),
      monthTotals(shiftMonth(current, -1), "-200.00"),
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
function wrap(children: ReactNode, data = fixture()) {
  const api = new Api("spending-test-user", async () => "fixture-token");
  const request = vi.spyOn(api, "request").mockResolvedValue(data as never);
  const view = render(
    <AppContext.Provider
      value={{ api, accounts: [], categories, timezone: "Asia/Singapore", revision: 0, refresh: vi.fn(), notify: vi.fn() }}
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
    <MonthlySpendingTrend report={report} fromMonth={shiftMonth(end, -11)} toMonth={end} />,
  );
  expect(container.querySelectorAll(".analysis-trend-row")).toHaveLength(12);
  expect(container.querySelectorAll(".analysis-bar.refund-heavy")).toHaveLength(1);
  expect(container.querySelectorAll(".analysis-trend-row:not(:has(.analysis-bar))").length).toBe(10);
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

it("groups extra expense categories precisely and explains incomplete FX amounts", () => {
  const report = fixture();
  report.category = Array.from({ length: 7 }, (_, i) => categoryTotals(String(i), "0.01"));
  report.known_gross_expenses = "0.07";
  report.gross_expenses = null;
  report.missing_conversion_count = 1;
  const { container } = wrap(<SpendingCategoryDonut report={report} />, report);
  expect(screen.getByText("其余分类合计")).toBeInTheDocument();
  expect(screen.getByText("28.6%")).toBeInTheDocument();
  expect(screen.getByText(/仅以已换算金额计算占比/)).toBeInTheDocument();
  expect(container.querySelectorAll(".analysis-category-list li")).toHaveLength(6);
});

it("requests one report for the selected range and rejects invalid custom dates", async () => {
  const current = today("Asia/Singapore").slice(0, 7);
  const user = userEvent.setup();
  const { request } = wrap(<SpendingAnalysis />);
  await screen.findByRole("heading", { name: "月度净支出趋势" });
  expect(request).toHaveBeenCalledWith(
    "/reports/spending?from=" + monthRange(shiftMonth(current, -11)).from
    + "&to=" + encodeURIComponent(monthRange(current).to),
  );
  await user.selectOptions(screen.getByRole("combobox", { name: "分析时间范围" }), "3");
  await waitFor(() => expect(request).toHaveBeenCalledWith(
    "/reports/spending?from=" + monthRange(shiftMonth(current, -2)).from
    + "&to=" + monthRange(current).to,
  ));
  await user.selectOptions(screen.getByRole("combobox", { name: "分析时间范围" }), "custom");
  fireEvent.change(screen.getByLabelText("分析开始月份"), { target: { value: current } });
  fireEvent.change(screen.getByLabelText("分析结束月份"), { target: { value: shiftMonth(current, -2) } });
  expect(await screen.findByText(/请选择有效的起止月份/)).toBeInTheDocument();
});

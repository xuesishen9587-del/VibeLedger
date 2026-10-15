import { useState } from "react";
import { useApp, useResource } from "../lib/context";
import { query } from "../lib/api";
import { money, monthRange, sumDecimal, today } from "../lib/format";
import type { Spending } from "../types";
import { Card, Empty, ErrorBox, Loading, Stat } from "./ui";

const PALETTE = ["#617f6b", "#a5b99a", "#cdb586", "#829eae", "#c59c91", "#a99ebc"];

export function shiftMonth(month: string, delta: number) {
  const [year, number] = month.split("-").map(Number);
  const date = new Date(Date.UTC(year, number - 1 + delta, 1));
  return String(date.getUTCFullYear()) + "-" + String(date.getUTCMonth() + 1).padStart(2, "0");
}

function monthCount(from: string, to: string) {
  const [fy, fm] = from.split("-").map(Number);
  const [ty, tm] = to.split("-").map(Number);
  return (ty - fy) * 12 + tm - fm + 1;
}

export function MonthlySpendingTrend({
  report,
  fromMonth,
  toMonth,
}: {
  report: Spending;
  fromMonth: string;
  toMonth: string;
}) {
  const byMonth = new Map(report.month.map((row) => [row.key, row]));
  const months = Array.from({ length: monthCount(fromMonth, toMonth) }, (_, i) =>
    shiftMonth(fromMonth, i),
  );
  const values = months.map((month) => byMonth.get(month)?.known_net_spending ?? "0.00");
  const maximum = Math.max(1, ...values.map((value) => Math.abs(Number(value))));
  return (
    <div className="analysis-trend" role="list" aria-label="月度净支出趋势">
      {months.map((month, index) => {
        const row = byMonth.get(month);
        const amount = Number(values[index]);
        const width = Math.min(49, (Math.abs(amount) / maximum) * 49);
        const incomplete = (row?.missing_conversion_count ?? 0) > 0;
        return (
          <div
            className="analysis-trend-row"
            role="listitem"
            key={month}
            aria-label={month + " 净支出 " + money(values[index], report.reporting_currency) + (incomplete ? "，部分交易缺汇率" : "")}
          >
            <span className="analysis-month">{month.slice(2).replace("-", "/")}</span>
            <div className="analysis-track" aria-hidden="true">
              <div className="analysis-zero" />
              {amount !== 0 && (
                <div
                  className={"analysis-bar" + (amount < 0 ? " refund-heavy" : "")}
                  style={{ left: String(amount < 0 ? 50 - width : 50) + "%", width: String(width) + "%" }}
                />
              )}
            </div>
            <span className="analysis-amount">
              {money(values[index], report.reporting_currency)}{incomplete ? " *" : ""}
            </span>
          </div>
        );
      })}
      <p className="muted small">净支出＝消费－退款；左侧柱表示该月退款超过消费。没有记录的月份显示零。</p>
    </div>
  );
}

export function SpendingCategoryDonut({ report }: { report: Spending }) {
  const { categories } = useApp();
  const total = Number(report.known_gross_expenses);
  const positive = report.category
    .filter((row) => Number(row.known_gross_expenses) > 0)
    .sort((a, b) => Number(b.known_gross_expenses) - Number(a.known_gross_expenses));
  if (!(total > 0) || positive.length === 0) {
    return (
      <Empty title="暂无可统计的消费金额">
        {report.missing_conversion_count
          ? "部分交易缺少汇率，目前无法绘制消费占比。"
          : "这个时间范围还没有消费记录。"}
      </Empty>
    );
  }
  const leading = positive.slice(0, 5).map((row) => ({
    key: row.key,
    name: categories.find((c) => c.id === row.key)?.name || "未分类",
    amount: row.known_gross_expenses,
  }));
  const remainder = positive.slice(5);
  if (remainder.length) {
    leading.push({
      key: "__remaining__",
      name: "其余分类合计",
      amount: sumDecimal(remainder.map((row) => row.known_gross_expenses)) || "0.00",
    });
  }
  let offset = 0;
  const slices = leading.map((row, index) => {
    const share = Math.max(0, Math.min(100 - offset, (Number(row.amount) / total) * 100));
    const start = offset;
    offset += share;
    return { ...row, color: PALETTE[index], percentage: Number(row.amount) / total * 100, start, end: offset };
  });
  const gradient = "conic-gradient(" + slices.map((s) =>
    s.color + " " + s.start + "% " + s.end + "%"
  ).join(", ") + ")";
  return (
    <>
      <div className="analysis-donut-layout">
        <div
          className="analysis-donut"
          style={{ background: gradient }}
          role="img"
          aria-label="消费总额按分类占比，具体金额和百分比见分类列表"
        >
          <div className="analysis-donut-center">
            <small>已换算消费</small>
            <strong>{money(report.known_gross_expenses, report.reporting_currency)}</strong>
          </div>
        </div>
        <ul className="analysis-category-list">
          {slices.map((slice) => (
            <li key={slice.key}>
              <span className="analysis-legend-dot" style={{ background: slice.color }} />
              <span className="analysis-category-name">{slice.name}</span>
              <strong>{money(slice.amount, report.reporting_currency)}</strong>
              <span className="analysis-share">{slice.percentage.toFixed(1)}%</span>
            </li>
          ))}
        </ul>
      </div>
      <p className="muted small">
        占比按消费总额计算，不扣退款；退款已在上方及月度净支出中单独体现。
        {report.missing_conversion_count > 0 ? " 含缺汇率记录时，仅以已换算金额计算占比。" : ""}
      </p>
    </>
  );
}

export function SpendingAnalysis() {
  const { timezone } = useApp();
  const currentMonth = today(timezone).slice(0, 7);
  const [windowSize, setWindowSize] = useState("12");
  const [customFrom, setCustomFrom] = useState(shiftMonth(currentMonth, -11));
  const [customTo, setCustomTo] = useState(currentMonth);
  const fromMonth = windowSize === "custom"
    ? customFrom
    : shiftMonth(currentMonth, 1 - Number(windowSize));
  const toMonth = windowSize === "custom" ? customTo : currentMonth;
  const validMonth = /^\d{4}-(0[1-9]|1[0-2])$/;
  const count = validMonth.test(fromMonth) && validMonth.test(toMonth)
    ? monthCount(fromMonth, toMonth)
    : 0;
  const rangeValid = count >= 1 && count <= 24 && toMonth <= currentMonth;
  const report = useResource<Spending>(
    rangeValid
      ? "/reports/spending" + query({ from: monthRange(fromMonth).from, to: monthRange(toMonth).to })
      : null,
  );
  return (
    <section className="spending-analysis" aria-label="支出分析">
      <div className="analysis-controls">
        <label className="inline-field">
          分析时间范围
          <select
            aria-label="分析时间范围"
            value={windowSize}
            onChange={(e) => setWindowSize(e.target.value)}
          >
            <option value="3">最近3个月</option>
            <option value="6">最近6个月</option>
            <option value="12">最近12个月</option>
            <option value="custom">自定义</option>
          </select>
        </label>
        {windowSize === "custom" && (
          <div className="analysis-custom-range">
            <label className="inline-field">
              开始
              <input type="month" aria-label="分析开始月份" value={customFrom}
                max={currentMonth} onChange={(e) => setCustomFrom(e.target.value)} />
            </label>
            <label className="inline-field">
              结束
              <input type="month" aria-label="分析结束月份" value={customTo}
                max={currentMonth} onChange={(e) => setCustomTo(e.target.value)} />
            </label>
          </div>
        )}
        <span className="muted small">{fromMonth} — {toMonth}</span>
      </div>
      {!rangeValid ? (
        <div className="notice" role="alert">请选择有效的起止月份，时间范围不得超过24个月，也不能晚于当前月份。</div>
      ) : report.loading ? (
        <Loading />
      ) : report.error ? (
        <ErrorBox error={report.error} retry={report.reload} />
      ) : report.data ? (
        <>
          <div className="stats-three">
            <Stat accent label={report.data.net_spending === null ? "已知净支出" : "净支出"}
              value={money(report.data.net_spending ?? report.data.known_net_spending, report.data.reporting_currency)}
              detail="消费减去退款" />
            <Stat label={report.data.gross_expenses === null ? "已知消费" : "消费合计"}
              value={money(report.data.gross_expenses ?? report.data.known_gross_expenses, report.data.reporting_currency)} />
            <Stat label="收到的退款"
              value={money(report.data.refunds ?? report.data.known_refunds, report.data.reporting_currency)} />
          </div>
          {report.data.missing_conversion_count > 0 && (
            <div className="notice">
              {report.data.missing_conversion_count} 笔交易缺少换算汇率，下面的金额均为已知小计，不能视为完整支出。
            </div>
          )}
          <div className="two-columns analysis-panels">
            <Card title="月度净支出趋势">
              <MonthlySpendingTrend report={report.data} fromMonth={fromMonth} toMonth={toMonth} />
            </Card>
            <Card title="消费分类占比">
              <SpendingCategoryDonut report={report.data} />
            </Card>
          </div>
        </>
      ) : null}
    </section>
  );
}

import type { Spending, Wealth } from "../types";
import { useApp } from "../lib/context";
import { money, day } from "../lib/format";
import { Empty, Tag } from "./ui";
export function CategoryChart({ report }: { report: Spending }) {
  const { categories } = useApp();
  const rows = report.category
    .filter((r) => Number(r.known_net_spending) !== 0)
    .sort(
      (a, b) => Number(b.known_net_spending) - Number(a.known_net_spending),
    );
  const max = Math.max(
    1,
    ...rows.map((r) => Math.abs(Number(r.known_net_spending))),
  );
  return rows.length ? (
    <div className="category-chart">
      {rows.slice(0, 7).map((r, i) => (
        <div className="category-bar" key={r.key}>
          <span className={"category-dot color-" + i} />
          <span>{categories.find((c) => c.id === r.key)?.name || "其他"}</span>
          <div className="bar-track">
            <div
              style={{
                width:
                  (Math.abs(Number(r.known_net_spending)) / max) * 100 + "%",
              }}
            />
          </div>
          <b>{money(r.known_net_spending, report.reporting_currency)}</b>
        </div>
      ))}
      <small className="muted">
        退款已抵扣 ·{" "}
        {report.missing_conversion_count
          ? "仅包含已有汇率的金额"
          : "按本月记录计算"}
        {rows.length > 7 ? " · 展示前 7 类" : ""}
      </small>
    </div>
  ) : (
    <Empty title="这个月还没有支出">记下第一笔，慢慢了解日常的花费。</Empty>
  );
}
export function WealthStats({ report }: { report: Wealth }) {
  const complete = report.coverage.complete;
  return (
    <div className="wealth-summary">
      <div className="net-worth">
        <span>
          {complete ? "家庭净资产" : "已知净资产"}{" "}
          <Tag tone="light">{report.reporting_currency}</Tag>
        </span>
        <strong>
          {money(
            complete ? report.net_worth : report.known_net_worth,
            report.reporting_currency,
          )}
        </strong>
        <small>根据各账户最近更新的余额</small>
      </div>
      <div>
        <span>我们的资产</span>
        <strong>
          {money(
            complete ? report.total_assets : report.known_assets,
            report.reporting_currency,
          )}
        </strong>
        <small>{complete ? "储蓄、钱包与投资" : "已记录部分"}</small>
      </div>
      <div>
        <span>待还的欠款</span>
        <strong>
          {money(
            complete ? report.total_liabilities : report.known_liabilities,
            report.reporting_currency,
          )}
        </strong>
        <small>信用卡与其他负债</small>
      </div>
    </div>
  );
}
export function WealthHistory({
  points,
}: {
  points: (Wealth & { as_of: string })[];
}) {
  const values = points.map((p) =>
    p.coverage.complete && p.net_worth !== null ? Number(p.net_worth) : null,
  );
  const actual = values.filter((n): n is number => n !== null);
  if (!actual.length)
    return (
      <Empty title="还没有完整的财富趋势">
        多记录几次账户余额，就能看到变化。
      </Empty>
    );
  const min = Math.min(...actual),
    max = Math.max(...actual),
    span = max - min || 1;
  const stamps = points.map((p) => Date.parse(p.as_of));
  const timeSpan = stamps.at(-1)! - stamps[0] || 1;
  const x = (i: number) => 48 + ((stamps[i] - stamps[0]) / timeSpan) * 640,
    y = (v: number) => 150 - ((v - min) / span) * 120;
  let path = "",
    previous: number | null = null;
  values.forEach((v, i) => {
    if (v === null) {
      previous = null;
      return;
    }
    path += previous === null ? `M ${x(i)} ${y(v)} ` : `H ${x(i)} V ${y(v)} `;
    previous = v;
  });
  return (
    <div className="history-chart">
      <svg
        viewBox="0 0 730 200"
        role="img"
        aria-label="已记录的净资产趋势，余额缺失期间留空"
      >
        <line x1="48" y1="150" x2="690" y2="150" stroke="#e4e8e1" />
        <text x="48" y="18">
          {money(max, points[0].reporting_currency)}
        </text>
        <path d={path} fill="none" stroke="#517862" strokeWidth="3" />
        {values.map((v, i) =>
          v === null ? null : (
            <circle key={i} cx={x(i)} cy={y(v)} r="3" fill="#517862">
              <title>
                {day(points[i].as_of)}：{money(v, points[i].reporting_currency)}
              </title>
            </circle>
          ),
        )}
        <text x="48" y="185">
          {day(points[0].as_of)}
        </text>
        <text x="690" y="185" textAnchor="end">
          {day(points.at(-1)?.as_of)}
        </text>
      </svg>
      <small className="muted">
        每次更新余额后形成一个变化点；信息不全的时段留空。
      </small>
    </div>
  );
}

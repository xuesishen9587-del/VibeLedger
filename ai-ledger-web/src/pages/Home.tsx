import {
  ArrowUpRight,
  FileUp,
  Plus,
  Wallet,
  ArrowRight,
  Clock3,
  Check,
} from "lucide-react";
import { useApp, useResource, navigate } from "../lib/context";
import { query } from "../lib/api";
import { money, today, monthRange, day } from "../lib/format";
import {
  Heading,
  Button,
  Card,
  Loading,
  ErrorBox,
  LinkButton,
  Tag,
  Empty,
} from "../components/ui";
import { WealthStats, CategoryChart } from "../components/Charts";
import type { Wealth, Spending, Review, Page, Transaction } from "../types";
export function Home() {
  const { timezone, accounts } = useApp();
  const range = monthRange(today(timezone).slice(0, 7));
  const wealth = useResource<Wealth>("/reports/wealth"),
    spending = useResource<Spending>(
      "/reports/spending" + query({ ...range, to: today(timezone) }),
    ),
    review = useResource<Review>("/review"),
    recent = useResource<Page<Transaction>>("/transactions?limit=5");
  const c = review.data?.counts;
  const count = c
    ? c.transactions +
      c.drafts +
      c.schedule_occurrences +
      c.unusual_investment_estimates
    : 0;
  return (
    <>
      <Heading
        title="总览"
        actions={
          <>
            <Button onClick={() => navigate("/statement")}>
              <FileUp size={16} />
              导入账单
            </Button>
            <Button kind="primary" onClick={() => navigate("/spending/new")}>
              <Plus size={16} />
              记一笔
            </Button>
          </>
        }
      >
        本月支出、家庭资产和待处理事项。
      </Heading>
      <ErrorBox error={wealth.error} retry={wealth.reload} />
      {wealth.loading ? (
        <Loading />
      ) : (
        wealth.data && (
          <>
            <WealthStats report={wealth.data} />
            {!wealth.data.coverage.complete && (
              <div className="notice subtle">
                还有 {wealth.data.coverage.missing_account_ids.length}{" "}
                个账户未记录余额
                {wealth.data.coverage.missing_fx_currencies.length
                  ? "，部分币种暂缺汇率"
                  : ""}
                。补齐后可查看完整的资产总额。
                <LinkButton to="/wealth">去更新</LinkButton>
              </div>
            )}
          </>
        )
      )}
      <div className="home-grid">
        <Card
          title="本月支出分类"
          action={<LinkButton to="/spending">查看支出</LinkButton>}
        >
          {spending.loading ? (
            <Loading />
          ) : spending.data ? (
            <>
              <div className="spending-head">
                <div>
                  <span className="muted">
                    {Number(range.from.slice(5, 7))} 月
                    {spending.data.net_spending === null ? "已知" : ""}净支出
                  </span>
                  <strong>
                    {money(
                      spending.data.net_spending ??
                        spending.data.known_net_spending,
                      spending.data.reporting_currency,
                    )}
                  </strong>
                </div>
                <span className="month-stamp">
                  {Number(range.from.slice(5, 7))}
                  <small>月</small>
                </span>
              </div>
              <CategoryChart report={spending.data} />
            </>
          ) : (
            <ErrorBox error={spending.error} retry={spending.reload} />
          )}
        </Card>
        <div className="home-aside">
          <Card title="待处理" className="attention-card">
            <div className="attention-icon">
              {count ? <Clock3 /> : <Check />}
            </div>
            <h3>
              {review.error
                ? "暂时读不到待办"
                : count
                  ? `${count} 项待处理`
                  : "暂无待处理事项"}
            </h3>
            <p>
              {count
                ? "有记录需要核对或补充。"
                : "新的待处理事项会出现在这里。"}
            </p>
            <LinkButton to="/review">
              {count ? "去处理" : "查看待处理"}
            </LinkButton>
          </Card>
          <Card className="soft-card">
            <Wallet size={24} />
            <h3>账户余额</h3>
            <p>余额变化后，可在家庭财富中更新记录。</p>
            <LinkButton to="/wealth">查看家庭财富</LinkButton>
          </Card>
        </div>
      </div>
      <div className="two-columns">
        <Card
          title="最近交易"
          action={<LinkButton to="/spending">全部记录</LinkButton>}
        >
          {recent.data?.items.length ? (
            <div className="recent-list">
              {recent.data.items.map((t) => (
                <div key={t.id}>
                  <span className="transaction-icon">
                    <ArrowUpRight size={17} />
                  </span>
                  <div>
                    <strong>{t.merchant || "日常记录"}</strong>
                    <small>
                      {day(t.occurred_on)} ·{" "}
                      {accounts.find((a) => a.id === t.account_id)?.name ||
                        "未选账户"}
                    </small>
                  </div>
                  <b
                    className={
                      t.transaction_type === "refund" ? "positive" : ""
                    }
                  >
                    {t.transaction_type === "refund" ? "+" : "−"}
                    {money(t.original_amount, t.original_currency)}
                  </b>
                </div>
              ))}
            </div>
          ) : recent.loading ? (
            <Loading />
          ) : recent.error ? (
            <ErrorBox error={recent.error} retry={recent.reload} />
          ) : (
            <Empty title="暂无交易记录" />
          )}
        </Card>
        <Card
          title="账户提醒"
          action={<LinkButton to="/wealth">全部账户</LinkButton>}
        >
          {wealth.data?.accounts
            .filter((a) => a.needs_update || a.balance === null)
            .slice(0, 4)
            .map((a) => (
              <a
                key={a.account_id}
                className="account-reminder"
                href="#/wealth"
              >
                <span>
                  <strong>{a.name}</strong>
                  <small>
                    {a.balance === null
                      ? "还没记过余额"
                      : `已经 ${a.age_days} 天没有更新余额`}
                  </small>
                </span>
                <Tag tone="amber">
                  {a.balance === null ? "待补充" : "该更新了"}
                </Tag>
                <ArrowRight size={15} />
              </a>
            ))}
          {wealth.data &&
            !wealth.data.accounts.some(
              (a) => a.needs_update || a.balance === null,
            ) && (
              <div className="calm-message">
                <Check size={22} />
                <p>暂无余额更新提醒。</p>
              </div>
            )}
        </Card>
      </div>
    </>
  );
}

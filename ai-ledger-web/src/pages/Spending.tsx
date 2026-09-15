import { useEffect, useState } from "react";
import { Plus, FileUp, Pencil, Search } from "lucide-react";
import { useApp, useResource, navigate } from "../lib/context";
import { query } from "../lib/api";
import {
  money,
  today,
  monthRange,
  day,
  kindLabel,
  currencies,
} from "../lib/format";
import {
  Heading,
  Button,
  Card,
  Stat,
  Field,
  Modal,
  ActionForm,
  Loading,
  Empty,
  ErrorBox,
  Tag,
} from "../components/ui";
import { Schedules, ScheduleEditor } from "./Schedules";
import type { Transaction, Page, Spending } from "../types";
export function TransactionEditor({
  record,
  onClose,
  onSaved,
}: {
  record?: Transaction;
  onClose: () => void;
  onSaved?: () => void;
}) {
  const { api, accounts, categories, timezone, refresh, notify } = useApp();
  const [kind, setKind] = useState(record?.transaction_type || "expense"),
    [voiding, setVoiding] = useState(false),
    [converting, setConverting] = useState(false);
  const available = categories.filter(
    (c) =>
      c.status === "active" &&
      c.category_type === (kind === "cash_income" ? "income" : "expense"),
  );
  if (converting && record)
    return <ScheduleEditor replacement={record} onClose={onClose} />;
  return (
    <Modal
      title={
        voiding ? "作废这笔记录" : record ? "修改这笔记录" : "记下一笔生活"
      }
      onClose={onClose}
    >
      <ActionForm
        label={voiding ? "确认作废" : record ? "保存修改" : "记好了"}
        onSave={async (f) => {
          if (voiding) {
            await api.command(
              `/transactions/${record!.id}/void`,
              "POST",
              {
                expected_version: record!.row_version,
                delete_reason: String(f.get("reason")),
              },
              "作废记录",
            );
          } else {
            const fields = {
              occurred_on: String(f.get("date")),
              original_amount: String(f.get("amount")).trim(),
              original_currency: String(f.get("currency")),
              category_id: String(f.get("category")),
              account_id: String(f.get("account")) || null,
              merchant: String(f.get("merchant")).trim() || null,
              remarks: String(f.get("remarks")).trim() || null,
              account_review_acknowledged: f.get("unknown_account") === "on",
            };
            await api.command(
              record ? "/transactions/" + record.id : "/transactions",
              record ? "PATCH" : "POST",
              record
                ? {
                    ...fields,
                    expected_version: record.row_version,
                    reason: "在家庭账本中修改",
                  }
                : { ...fields, transaction_type: kind },
              record ? "修改记录" : "新增记录",
            );
          }
          notify(voiding ? "这笔记录已作废" : "记好了");
          refresh();
          onSaved?.();
          onClose();
        }}
      >
        {voiding ? (
          <>
            <p>作废后，这笔金额将从支出统计中移除，历史记录仍可追溯。</p>
            <Field label="作废原因">
              <input name="reason" required />
            </Field>
          </>
        ) : (
          <>
            <div className="form-grid">
              <Field label="性质">
                <select
                  value={kind}
                  onChange={(e) => setKind(e.target.value)}
                  disabled={!!record}
                >
                  <option value="expense">消费</option>
                  <option value="refund">退款</option>
                  <option value="cash_income">收入</option>
                </select>
              </Field>
              <Field label="日期">
                <input
                  name="date"
                  type="date"
                  required
                  defaultValue={record?.occurred_on || today(timezone)}
                  max={today(timezone)}
                />
              </Field>
              <Field label="金额">
                <input
                  name="amount"
                  inputMode="decimal"
                  required
                  defaultValue={record?.original_amount || ""}
                  placeholder="0.00"
                />
              </Field>
              <Field label="币种">
                <select
                  name="currency"
                  defaultValue={record?.original_currency || "CNY"}
                >
                  {currencies.map((c) => (
                    <option key={c}>{c}</option>
                  ))}
                </select>
              </Field>
            </div>
            <Field label="商户 / 用途">
              <input
                name="merchant"
                maxLength={240}
                defaultValue={record?.merchant || ""}
                placeholder="比如：两个人的晚餐"
              />
            </Field>
            <div className="form-grid">
              <Field label="分类">
                <select
                  name="category"
                  required
                  defaultValue={record?.category_id || ""}
                >
                  <option value="" disabled>
                    选个分类
                  </option>
                  {available.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="账户">
                <select name="account" defaultValue={record?.account_id || ""}>
                  <option value="">暂不选择</option>
                  {accounts
                    .filter(
                      (a) =>
                        a.status === "active" || a.id === record?.account_id,
                    )
                    .map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name}
                      </option>
                    ))}
                </select>
              </Field>
            </div>
            <label className="check">
              <input
                type="checkbox"
                name="unknown_account"
                defaultChecked={record?.account_review_acknowledged}
              />
              没有选账户时，这笔暂时不再提醒
            </label>
            <Field label={kind === "refund" ? "退款说明" : "备注（可选）"}>
              <input
                name="remarks"
                required={kind === "refund"}
                defaultValue={record?.remarks || ""}
              />
            </Field>
            {record && (
              <Button kind="danger ghost" onClick={() => setVoiding(true)}>
                作废这笔记录
              </Button>
            )}
            {record?.transaction_type === "expense" &&
              !record.schedule_occurrence_id && (
                <Button onClick={() => setConverting(true)}>
                  把这笔全额消费改为分期
                </Button>
              )}
          </>
        )}
      </ActionForm>
    </Modal>
  );
}
export function SpendingPage() {
  const { categories, accounts, timezone } = useApp();
  const [month, setMonth] = useState(today(timezone).slice(0, 7)),
    [category, setCategory] = useState(""),
    [account, setAccount] = useState(""),
    [search, setSearch] = useState(""),
    [cursor, setCursor] = useState<string | null>(null),
    [edit, setEdit] = useState<Transaction | true | null>(
      location.hash.includes("/new") ? true : null,
    ),
    [tab, setTab] = useState("transactions");
  const range = monthRange(month);
  const report = useResource<Spending>("/reports/spending" + query(range));
  const records = useResource<Page<Transaction>>(
    "/transactions" +
      query({
        ...range,
        category_id: category,
        account_id: account,
        merchant: search,
        cursor,
        limit: 30,
      }),
  );
  useEffect(() => {
    const listener = () => {
      if (location.hash.includes("/new")) setEdit(true);
    };
    window.addEventListener("hashchange", listener);
    return () => window.removeEventListener("hashchange", listener);
  }, []);
  const change = (fn: () => void) => {
    setCursor(null);
    fn();
  };
  return (
    <>
      <Heading
        eyebrow="EVERYDAY MOMENTS"
        title="日常支出"
        actions={
          <>
            <Button onClick={() => navigate("/statement")}>
              <FileUp size={16} />
              导入账单
            </Button>
            <Button kind="primary" onClick={() => setEdit(true)}>
              <Plus size={16} />
              记一笔
            </Button>
          </>
        }
      >
        每一笔，都是生活的一部分。
      </Heading>
      <div className="toolbar">
        <div className="tabs">
          <button
            className={tab === "transactions" ? "selected" : ""}
            onClick={() => setTab("transactions")}
          >
            支出记录
          </button>
          <button
            className={tab === "schedules" ? "selected" : ""}
            onClick={() => setTab("schedules")}
          >
            月度计划
          </button>
        </div>
        <label className="inline-field">
          月份
          <input
            type="month"
            aria-label="月份"
            value={month}
            onChange={(e) =>
              change(() =>
                setMonth(e.target.value || today(timezone).slice(0, 7)),
              )
            }
          />
        </label>
      </div>
      {tab === "schedules" ? (
        <Schedules />
      ) : (
        <>
          <ErrorBox error={report.error} retry={report.reload} />
          {report.data && (
            <div className="stats-three">
              <Stat
                accent
                label={
                  report.data.net_spending === null
                    ? "已知净支出"
                    : "本月净支出"
                }
                value={money(
                  report.data.net_spending ?? report.data.known_net_spending,
                  report.data.reporting_currency,
                )}
                detail="消费减去退款"
              />
              <Stat
                label="消费合计"
                value={money(
                  report.data.gross_expenses ??
                    report.data.known_gross_expenses,
                  report.data.reporting_currency,
                )}
              />
              <Stat
                label="收到的退款"
                value={money(
                  report.data.refunds ?? report.data.known_refunds,
                  report.data.reporting_currency,
                )}
              />
            </div>
          )}
          {!!report.data?.missing_conversion_count && (
            <div className="notice">
              还有 {report.data.missing_conversion_count}{" "}
              笔记录等待汇率，合计暂未包含这些金额。
            </div>
          )}
          <Card>
            <div className="filters">
              <label className="search">
                <Search size={16} />
                <input
                  aria-label="搜索商户"
                  value={search}
                  onChange={(e) => change(() => setSearch(e.target.value))}
                  placeholder="找一笔消费…"
                />
              </label>
              <select
                aria-label="分类筛选"
                value={category}
                onChange={(e) => change(() => setCategory(e.target.value))}
              >
                <option value="">全部分类</option>
                {categories.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
              <select
                aria-label="账户筛选"
                value={account}
                onChange={(e) => change(() => setAccount(e.target.value))}
              >
                <option value="">全部账户</option>
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </select>
            </div>
            {records.loading ? (
              <Loading />
            ) : records.error ? (
              <ErrorBox error={records.error} retry={records.reload} />
            ) : records.data?.items.length ? (
              <>
                <div className="table-wrap">
                  <table className="ledger-table">
                    <thead>
                      <tr>
                        <th>日期</th>
                        <th>商户 / 用途</th>
                        <th>分类</th>
                        <th>账户</th>
                        <th className="amount">金额</th>
                        <th>
                          <span className="sr-only">操作</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {records.data.items.map((t) => (
                        <tr key={t.id}>
                          <td className="muted">{day(t.occurred_on)}</td>
                          <td>
                            <strong>
                              {t.merchant || kindLabel[t.transaction_type]}
                            </strong>
                            {t.transaction_type !== "expense" && (
                              <Tag>{kindLabel[t.transaction_type]}</Tag>
                            )}
                          </td>
                          <td>
                            {categories.find((c) => c.id === t.category_id)
                              ?.name || "待分类"}
                            {t.category_uncertain && (
                              <span className="status-dot" title="分类待补充" />
                            )}
                          </td>
                          <td className="muted">
                            {accounts.find((a) => a.id === t.account_id)
                              ?.name || "暂未选择"}
                          </td>
                          <td
                            className={
                              "amount " +
                              (t.transaction_type === "refund"
                                ? "positive"
                                : "")
                            }
                          >
                            {t.transaction_type === "refund" ? "+" : ""}
                            {money(t.original_amount, t.original_currency)}
                            <small>{t.original_currency}</small>
                          </td>
                          <td>
                            <button
                              className="icon-button"
                              aria-label={"修改 " + (t.merchant || "记录")}
                              onClick={() => setEdit(t)}
                            >
                              <Pencil size={15} />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="pagination">
                  <span>本页 {records.data.items.length} 笔</span>
                  {cursor && (
                    <Button onClick={() => setCursor(null)}>回到第一页</Button>
                  )}
                  <Button
                    disabled={!records.data.next_cursor}
                    onClick={() => setCursor(records.data!.next_cursor)}
                  >
                    下一页
                  </Button>
                </div>
              </>
            ) : (
              <Empty title="这个月份还没有记录">
                可以记一笔，或直接导入一整份账单。
              </Empty>
            )}
          </Card>
        </>
      )}
      {edit && (
        <TransactionEditor
          record={edit === true ? undefined : edit}
          onClose={() => {
            setEdit(null);
            if (location.hash.includes("/new")) navigate("/spending");
          }}
        />
      )}
    </>
  );
}

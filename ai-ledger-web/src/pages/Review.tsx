import { useState } from "react";
import {
  Inbox,
  FileText,
  Image,
  ChevronRight,
  Check,
  CalendarDays,
} from "lucide-react";
import { useApp, useResource, navigate } from "../lib/context";
import { query } from "../lib/api";
import { money, day, currencies, sumDecimal } from "../lib/format";
import { errorMessage } from "../lib/errors";
import {
  Heading,
  Card,
  Empty,
  Loading,
  ErrorBox,
  Button,
  Tag,
  Modal,
  Field,
  ActionForm,
} from "../components/ui";
import { TransactionEditor } from "./Spending";
import { InvestmentEditor } from "./Wealth";
import { ScheduleEditor } from "./Schedules";
import type {
  Receipt,
  Page,
  Review,
  Transaction,
  Investment,
  BalanceRow,
  StatementDraft,
  Schedule,
} from "../types";
type ExpenseDraft = {
  payment_mode?: "one_off" | "installment" | null;
  total_periods?: number | null;
  action?: "record_full_purchase" | "use_schedule_period" | null;
  schedule_id?: string | null;
  period_no?: number | null;
  intent: string;
  original_amount: string;
  original_currency: string;
  occurred_on: string;
  merchant: string;
  from_account?: { id: string };
  category?: { id: string };
  remarks?: string;
};
type CaptureDraft = { rows: BalanceRow[]; acknowledge_evidence: boolean };
type AnyDraft = StatementDraft | ExpenseDraft | CaptureDraft;
function CaptureEditor({
  receipt,
  onClose,
}: {
  receipt: Receipt<ExpenseDraft | CaptureDraft>;
  onClose: () => void;
}) {
  const { api, accounts, categories, refresh, notify } = useApp();
  const [current, setCurrent] = useState(receipt),
    [discard, setDiscard] = useState(false);
  const balance = "rows" in current.draft;
  const [rows, setRows] = useState<BalanceRow[]>(
    balance ? (current.draft as CaptureDraft).rows : [],
  );
  const d = current.draft as ExpenseDraft;
  const [paymentMode, setPaymentMode] = useState(d.payment_mode || ""),
    [action, setAction] = useState(d.action || ""),
    [converting, setConverting] = useState(false);
  const plans = useResource<Page<Schedule>>(
    !balance && action === "use_schedule_period"
      ? "/spending-schedules?limit=200"
      : null,
  );
  if (converting)
    return (
      <ScheduleEditor
        sourceDraft={{
          request_id: current.request_id,
          row_version: current.row_version,
          merchant: d.merchant,
          currency: d.original_currency,
          category_id: d.category?.id,
          account_id: d.from_account?.id,
          periods: d.total_periods || undefined,
        }}
        onClose={onClose}
      />
    );
  return (
    <Modal
      title={
        discard
          ? "放弃这次识别？"
          : balance
            ? "核对截图里的余额"
            : "核对这笔消费"
      }
      onClose={onClose}
    >
      {current.warnings.map((w, i) => (
        <p className="notice warning" key={i}>
          {errorMessage(w.code)}
        </p>
      ))}
      <ActionForm
        label={
          discard ? "确认放弃" : balance ? "确认并保存余额" : "确认并保存消费"
        }
        onSave={async (f) => {
          const base = "/ingestion-requests/" + current.request_id;
          if (discard) {
            const result = await api.request<Receipt>(base + "/reject", {
              method: "POST",
              body: JSON.stringify({ expected_version: current.row_version }),
            });
            refresh();
            notify(
              result.status === "committed"
                ? "这次识别已经保存，请在账本中查看"
                : "已放弃这次识别",
            );
            onClose();
            return;
          } else {
            const body = balance
              ? {
                  expected_version: current.row_version,
                  rows: rows.map((r) => {
                    const a = accounts.find((a) => a.id === r.account_id);
                    return {
                      row_id: r.row_id,
                      selected: r.selected,
                      account_id: r.account_id,
                      balance: r.balance,
                      currency: a?.currency || r.currency,
                      as_of: r.as_of,
                      time_basis: r.time_basis,
                      expected_account_version: a?.row_version ?? null,
                      expected_latest_snapshot_id:
                        a?.latest_snapshot?.id || null,
                      exclusion_reason: r.selected
                        ? null
                        : "本次不更新这个余额",
                    };
                  }),
                  acknowledge_evidence: true,
                }
              : {
                  expected_version: current.row_version,
                  original_amount: String(f.get("amount")),
                  original_currency: String(f.get("currency")),
                  occurred_on: String(f.get("date")),
                  merchant: String(f.get("merchant")),
                  from_account_id: String(f.get("account")) || null,
                  category_id: String(f.get("category")) || null,
                  intent: String(f.get("intent")),
                  payment_mode: paymentMode,
                  ...(paymentMode === "installment"
                    ? {
                        action,
                        ...(action === "use_schedule_period"
                          ? {
                              schedule_id: String(f.get("schedule")),
                              expected_schedule_version: plans.data?.items.find(
                                (s) => s.id === f.get("schedule"),
                              )?.row_version,
                              period_no: Number(f.get("period")),
                            }
                          : {}),
                      }
                    : {}),
                  remarks: String(f.get("remarks")) || null,
                };
            let result = await api.request<typeof receipt>(base + "/draft", {
              method: "PATCH",
              body: JSON.stringify(body),
            });
            setCurrent(result);
            if (result.warnings.length)
              throw new Error("还有几处需要核对，已在上方列出。");
            result = await api.request<typeof receipt>(base + "/confirm", {
              method: "POST",
              body: JSON.stringify({ expected_version: result.row_version }),
            });
            if (result.status !== "committed") {
              if (result.status === "needs_confirmation") setCurrent(result);
              throw new Error("记录有新的变化，请再核对一下。");
            }
          }
          refresh();
          notify(discard ? "已放弃这次识别" : "保存好了");
          onClose();
        }}
      >
        {discard ? (
          <p>这次尚未保存的内容不会计入账本。</p>
        ) : balance ? (
          <>
            {rows.map((r, i) => (
              <section className="balance-edit-row" key={r.row_id}>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={r.selected}
                    onChange={(e) =>
                      setRows((s) =>
                        s.map((x, j) =>
                          j === i ? { ...x, selected: e.target.checked } : x,
                        ),
                      )
                    }
                  />
                  {r.label || "识别到的余额"}
                </label>
                <div className="form-grid">
                  <Field label="对应账户">
                    <select
                      value={r.account_id || ""}
                      onChange={(e) =>
                        setRows((s) =>
                          s.map((x, j) =>
                            j === i ? { ...x, account_id: e.target.value } : x,
                          ),
                        )
                      }
                    >
                      <option value="">请选择</option>
                      {accounts
                        .filter((a) => a.status === "active")
                        .map((a) => (
                          <option key={a.id} value={a.id}>
                            {a.name}
                          </option>
                        ))}
                    </select>
                  </Field>
                  <Field label="余额（欠款填负数）">
                    <input
                      value={r.balance || ""}
                      onChange={(e) =>
                        setRows((s) =>
                          s.map((x, j) =>
                            j === i ? { ...x, balance: e.target.value } : x,
                          ),
                        )
                      }
                    />
                  </Field>
                </div>
                <Field label="余额时间">
                  <input
                    value={r.as_of || ""}
                    onChange={(e) =>
                      setRows((s) =>
                        s.map((x, j) =>
                          j === i ? { ...x, as_of: e.target.value } : x,
                        ),
                      )
                    }
                  />
                </Field>
              </section>
            ))}
            <label className="check">
              <input type="checkbox" required />
              我已核对账户范围与金额，账户间没有重复包含
            </label>
          </>
        ) : (
          <>
            <Field label="这笔金额怎样记？">
              <select
                value={paymentMode}
                required
                onChange={(e) => setPaymentMode(e.target.value)}
              >
                <option value="">请选择</option>
                <option value="one_off">一次性消费</option>
                <option value="installment">分期消费</option>
              </select>
            </Field>
            {paymentMode === "installment" && (
              <>
                <Field label="分期的处理方式">
                  <select
                    value={action}
                    required
                    onChange={(e) => setAction(e.target.value)}
                  >
                    <option value="">请选择</option>
                    <option value="record_full_purchase">
                      这笔是全额，现在计入全部支出
                    </option>
                    <option value="use_schedule_period">
                      这笔对应已有计划的一期
                    </option>
                  </select>
                </Field>
                {d.payment_mode === "installment" && (
                  <Button onClick={() => setConverting(true)}>
                    按每期金额新建分期计划
                  </Button>
                )}
                {action === "use_schedule_period" && (
                  <>
                    <ErrorBox error={plans.error} />
                    <Field label="对应的分期计划">
                      <select
                        name="schedule"
                        required
                        defaultValue={d.schedule_id || ""}
                      >
                        <option value="">请选择</option>
                        {plans.data?.items.map((s) => (
                          <option key={s.id} value={s.id}>
                            {s.name}
                          </option>
                        ))}
                      </select>
                    </Field>
                    {plans.data?.next_cursor && (
                      <p className="notice">只列出前 200 个计划。</p>
                    )}
                    <Field label="第几期">
                      <input
                        name="period"
                        type="number"
                        required
                        min="1"
                        defaultValue={d.period_no || 1}
                      />
                    </Field>
                  </>
                )}
              </>
            )}
            <div className="form-grid">
              <Field label="金额">
                <input
                  name="amount"
                  defaultValue={d.original_amount || ""}
                  required
                />
              </Field>
              <Field label="币种">
                <select
                  name="currency"
                  defaultValue={d.original_currency || ""}
                >
                  <option value="">请选择</option>
                  {currencies.map((c) => (
                    <option key={c}>{c}</option>
                  ))}
                </select>
              </Field>
              <Field label="交易日期">
                <input
                  name="date"
                  type="date"
                  defaultValue={d.occurred_on || ""}
                  required
                />
              </Field>
              <Field label="性质">
                <select name="intent" defaultValue={d.intent || "unknown"}>
                  <option value="unknown">待确认</option>
                  <option value="expense">消费</option>
                  <option value="refund">退款</option>
                  <option value="transfer">转账</option>
                  <option value="repayment">还款</option>
                </select>
              </Field>
            </div>
            <Field label="商户">
              <input name="merchant" defaultValue={d.merchant || ""} />
            </Field>
            <Field label="账户">
              <select name="account" defaultValue={d.from_account?.id || ""}>
                <option value="">暂不选择</option>
                {accounts
                  .filter((a) => a.status === "active")
                  .map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                    </option>
                  ))}
              </select>
            </Field>
            <Field label="分类">
              <select name="category" defaultValue={d.category?.id || ""}>
                <option value="">待分类</option>
                {categories
                  .filter(
                    (c) =>
                      c.category_type === "expense" && c.status === "active",
                  )
                  .map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
              </select>
            </Field>
            <Field label="备注">
              <input name="remarks" defaultValue={d.remarks || ""} />
            </Field>
          </>
        )}
        {!discard && (
          <Button kind="ghost danger" onClick={() => setDiscard(true)}>
            放弃这次识别
          </Button>
        )}
      </ActionForm>
    </Modal>
  );
}
type Occurrence = {
  id: string;
  schedule_id: string;
  period_no: number;
  due_on: string;
  amount: string;
  currency: string;
  row_version: number;
};
function OccurrenceEditor({
  item,
  onClose,
}: {
  item: Occurrence;
  onClose: () => void;
}) {
  const { api, refresh, notify } = useApp();
  const [action, setAction] = useState("link_existing"),
    [cursor, setCursor] = useState<string | null>(null);
  const schedule = useResource<Schedule>(
    `/spending-schedules/${item.schedule_id}`,
  );
  const candidates = useResource<Page<Transaction>>(
    action === "link_existing"
      ? "/transactions" +
          query({
            from: item.due_on,
            to: item.due_on,
            transaction_type: "expense",
            limit: 50,
            cursor,
          })
      : null,
  );
  const choices = (candidates.data?.items || []).filter(
    (t) =>
      !t.schedule_occurrence_id &&
      t.original_currency === item.currency &&
      sumDecimal([t.original_amount]) === sumDecimal([item.amount]),
  );
  return (
    <Modal title="这期消费是否已经记过？" onClose={onClose}>
      <p>
        {schedule.data?.name || "月度计划"} · 第 {item.period_no} 期 ·{" "}
        {day(item.due_on)} · {money(item.amount, item.currency)}
      </p>
      <ActionForm
        label="确认处理"
        onSave={async (f) => {
          const target = choices.find((t) => t.id === f.get("transaction"));
          await api.command(
            `/schedule-occurrences/${item.id}/resolve`,
            "POST",
            {
              expected_version: item.row_version,
              action,
              reason: String(f.get("reason")) || null,
              ...(action === "link_existing"
                ? {
                    transaction_id: target?.id,
                    expected_transaction_version: target?.row_version,
                  }
                : {}),
            },
            "处理到期计划",
          );
          refresh();
          notify("这期计划已整理好");
          onClose();
        }}
      >
        <Field label="处理方式">
          <select value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="link_existing">已经记过，关联已有消费</option>
            <option value="skip">这期跳过</option>
            <option value="record_separately">这是另一笔消费，单独记账</option>
          </select>
        </Field>
        {action === "link_existing" && (
          <>
            <ErrorBox error={candidates.error} />
            {candidates.loading ? (
              <Loading />
            ) : (
              <Field label="对应的已有消费">
                <select name="transaction" required>
                  <option value="">请选择日期、金额和币种一致的消费</option>
                  {choices.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.merchant || "日常消费"} ·{" "}
                      {money(t.original_amount, t.original_currency)}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            {!candidates.loading && !choices.length && (
              <p className="notice">
                这一页没有可关联的消费。可以查看下一页，或核对后选择其他处理方式。
              </p>
            )}
            {candidates.data?.next_cursor && (
              <Button onClick={() => setCursor(candidates.data!.next_cursor)}>
                查看更多候选消费
              </Button>
            )}
            {cursor && (
              <Button onClick={() => setCursor(null)}>回到第一页</Button>
            )}
          </>
        )}
        <Field label="说明">
          <input name="reason" required={action !== "link_existing"} />
        </Field>
      </ActionForm>
    </Modal>
  );
}
export function ReviewPage({ identity }: { identity?: string }) {
  const { accounts } = useApp();
  const [tab, setTab] = useState("draft"),
    [cursor, setCursor] = useState<string | null>(null),
    [edit, setEdit] = useState<Transaction | null>(null),
    [investment, setInvestment] = useState<Investment | null>(null),
    [occurrence, setOccurrence] = useState<Occurrence | null>(null);
  const summary = useResource<Review>("/review");
  const drafts = useResource<Page<Receipt<AnyDraft>>>(
    tab === "draft"
      ? "/ingestion-requests" + query({ limit: 30, cursor })
      : null,
  );
  const transactions = useResource<Review>(
    tab === "transaction"
      ? "/review" + query({ section: tab, cursor, limit: 30 })
      : null,
  );
  const investments = useResource<Review<Investment>>(
    tab === "investment"
      ? "/review" + query({ section: tab, cursor, limit: 30 })
      : null,
  );
  const schedules = useResource<Review<Occurrence>>(
    tab === "schedule"
      ? "/review" + query({ section: tab, cursor, limit: 30 })
      : null,
  );
  const detail = useResource<Receipt<ExpenseDraft | CaptureDraft>>(
    identity ? "/ingestion-requests/" + identity : null,
  );
  const c = summary.data?.counts;
  const total = c
    ? c.drafts +
      c.transactions +
      c.schedule_occurrences +
      c.unusual_investment_estimates
    : 0;
  const active =
    tab === "draft"
      ? drafts
      : tab === "transaction"
        ? transactions
        : tab === "investment"
          ? investments
          : schedules;
  return (
    <>
      <Heading
        eyebrow="A LITTLE CARE FOR OUR HOME"
        title="待处理"
        actions={<Tag tone="amber">{total} 件小事</Tag>}
      >
        只把真正需要你看一眼的事情，留在这里。
      </Heading>
      <Card>
        <div className="tabs review-tabs">
          {[
            ["draft", "账单与截图", c?.drafts],
            ["transaction", "补充资料", c?.transactions],
            ["schedule", "月度计划", c?.schedule_occurrences],
            ["investment", "投资变化", c?.unusual_investment_estimates],
          ].map(([key, label, count]) => (
            <button
              className={tab === key ? "selected" : ""}
              key={key}
              onClick={() => {
                setTab(String(key));
                setCursor(null);
              }}
            >
              {label} <span>{count ?? 0}</span>
            </button>
          ))}
        </div>
        <ErrorBox error={active.error || summary.error} retry={active.reload} />
        {active.loading ? (
          <Loading />
        ) : active.data?.items.length ? (
          <>
            {tab === "draft" &&
              drafts.data?.items.map((r) => {
                const statement = "lines" in r.draft;
                const balance = "rows" in r.draft;
                return (
                  <button
                    key={r.request_id}
                    className="inbox-row"
                    onClick={() =>
                      navigate(
                        (statement ? "/statement/" : "/review/") + r.request_id,
                      )
                    }
                  >
                    {statement ? <FileText /> : <Image />}
                    <div>
                      <strong>
                        {statement
                          ? (accounts.find(
                              (a) =>
                                a.id === (r.draft as StatementDraft).account_id,
                            )?.name || "账户") + "账单"
                          : balance
                            ? "截图里的账户余额"
                            : (r.draft as ExpenseDraft).merchant ||
                              "一笔待保存的消费"}
                      </strong>
                      <small>
                        {statement
                          ? `${(r.draft as StatementDraft).lines.length} 笔交易，核对后整单导入`
                          : balance
                            ? "请选择对应账户，并核对余额"
                            : "还没有入账，请补齐需要确认的信息"}
                      </small>
                    </div>
                    <Tag tone="amber">待保存</Tag>
                    <ChevronRight size={18} />
                  </button>
                );
              })}
            {tab === "transaction" &&
              transactions.data?.items.map((t) => (
                <button
                  key={t.id}
                  className="inbox-row"
                  onClick={() => setEdit(t)}
                >
                  <Inbox />
                  <div>
                    <strong>
                      {t.merchant || "日常消费"} ·{" "}
                      {money(t.original_amount, t.original_currency)}
                    </strong>
                    <small>
                      {[
                        !t.account_id && !t.account_review_acknowledged
                          ? "补充付款账户"
                          : "",
                        t.category_uncertain ? "选个分类" : "",
                      ]
                        .filter(Boolean)
                        .join("，")}{" "}
                      · 已经入账，补充后不会再记一次
                    </small>
                  </div>
                  <ChevronRight size={18} />
                </button>
              ))}
            {tab === "investment" &&
              investments.data?.items.map((i) => (
                <button
                  key={i.id}
                  className="inbox-row"
                  onClick={() => setInvestment(i)}
                >
                  <div>
                    <strong>
                      {i.account_name} · {money(i.gain, i.currency)}
                    </strong>
                    <small>
                      {day(i.period_start)} — {day(i.period_end)}
                      ，这段时间有额外投入或取出吗？
                    </small>
                  </div>
                  <ChevronRight size={18} />
                </button>
              ))}
            {tab === "schedule" &&
              schedules.data?.items.map((o) => (
                <button
                  key={o.id}
                  className="inbox-row"
                  onClick={() => setOccurrence(o)}
                >
                  <CalendarDays />
                  <div>
                    <strong>
                      第 {o.period_no} 期 · {money(o.amount, o.currency)}
                    </strong>
                    <small>{day(o.due_on)} 到期，可能与已有消费重复</small>
                  </div>
                  <ChevronRight size={18} />
                </button>
              ))}
          </>
        ) : (
          !active.error && (
            <Empty title="这一栏都整理好了">
              <Check size={18} /> 把时间留给生活吧。
            </Empty>
          )
        )}
        <div className="pagination">
          {cursor && (
            <Button onClick={() => setCursor(null)}>回到第一页</Button>
          )}
          {active.data?.next_cursor && (
            <Button onClick={() => setCursor(active.data!.next_cursor)}>
              下一页
            </Button>
          )}
        </div>
      </Card>
      {edit && (
        <TransactionEditor record={edit} onClose={() => setEdit(null)} />
      )}{" "}
      {investment && (
        <InvestmentEditor
          item={investment}
          onClose={() => setInvestment(null)}
        />
      )}{" "}
      {detail.error && <ErrorBox error={detail.error} retry={detail.reload} />}{" "}
      {identity && detail.data?.status === "needs_confirmation" && (
        <CaptureEditor
          key={identity}
          receipt={detail.data}
          onClose={() => navigate("/review")}
        />
      )}{" "}
      {identity &&
        detail.data &&
        detail.data.status !== "needs_confirmation" && (
          <Card>
            <p>这条记录已经处理过了。</p>
            <Button onClick={() => navigate("/review")}>返回待处理</Button>
          </Card>
        )}{" "}
      {occurrence && (
        <OccurrenceEditor
          item={occurrence}
          onClose={() => setOccurrence(null)}
        />
      )}
    </>
  );
}

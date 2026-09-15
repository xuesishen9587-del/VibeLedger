import { useState } from "react";
import { CalendarDays, Plus } from "lucide-react";
import { useApp, useResource } from "../lib/context";
import { query } from "../lib/api";
import { money, today, day, currencies } from "../lib/format";
import type { Page, Schedule, Transaction } from "../types";
import {
  Card,
  Button,
  Empty,
  Loading,
  ErrorBox,
  Tag,
  Modal,
  Field,
  ActionForm,
} from "../components/ui";
type Preview = {
  acknowledged_due_through: string;
  due_total: string;
  currency: string;
  due_periods: { period_no: number; due_on: string; duplicate_ids: string[] }[];
  upcoming_dates: string[];
};
export type SourceDraft = {
  request_id: string;
  row_version: number;
  merchant?: string;
  currency?: string;
  category_id?: string;
  account_id?: string;
  periods?: number;
};
export function ScheduleEditor({
  item,
  replacement,
  sourceDraft,
  onClose,
}: {
  item?: Schedule;
  replacement?: Transaction;
  sourceDraft?: SourceDraft;
  onClose: () => void;
}) {
  const { api, accounts, categories, timezone, refresh, notify } = useApp();
  const [kind, setKind] = useState(
      item?.kind || (replacement || sourceDraft ? "installment" : "recurring"),
    ),
    [preview, setPreview] = useState<{
      body: Record<string, unknown>;
      result: Preview;
    } | null>(null);
  const [savedForm, setSavedForm] = useState<Record<string, string>>({});
  const saved = (key: string, fallback: string | number) =>
    savedForm[key] ?? fallback;
  return (
    <Modal
      title={
        replacement
          ? "把这笔全额消费改为分期"
          : item
            ? "修改月度计划"
            : "新建月度计划"
      }
      onClose={onClose}
    >
      {preview ? (
        <>
          <p>
            截至 {day(preview.result.acknowledged_due_through)}，有{" "}
            {preview.result.due_periods.length} 期已到期，合计{" "}
            {money(preview.result.due_total, preview.result.currency)}。
          </p>
          <p className="notice">
            保存后会补记已到期的支出。可能重复的期数会放入待处理。
          </p>
          {replacement && (
            <p className="notice warning">
              原来的全额消费会被替换为分期记录。这样不会同时统计全额与每期消费。
            </p>
          )}
          <ActionForm
            label="确认并保存计划"
            onSave={async () => {
              await api.command(
                "/spending-schedules",
                "POST",
                {
                  ...preview.body,
                  acknowledged_due_through:
                    preview.result.acknowledged_due_through,
                  ...(replacement
                    ? {
                        replaces_transaction_id: replacement.id,
                        expected_transaction_version: replacement.row_version,
                      }
                    : {}),
                  ...(sourceDraft
                    ? {
                        source_draft_request_id: sourceDraft.request_id,
                        expected_draft_version: sourceDraft.row_version,
                      }
                    : {}),
                },
                "保存月度计划",
              );
              refresh();
              notify("月度计划已保存");
              onClose();
            }}
          >
            <Button onClick={() => setPreview(null)}>返回修改</Button>
            <label className="check">
              <input type="checkbox" required />
              我已核对这些到期金额
            </label>
          </ActionForm>
        </>
      ) : (
        <ActionForm
          label={item ? "保存修改" : "预览记账安排"}
          onSave={async (f) => {
            setSavedForm(
              Object.fromEntries(
                [...f.entries()].map(([k, v]) => [k, String(v)]),
              ),
            );
            const body = {
              name: String(f.get("name")),
              kind,
              amount_per_period: String(f.get("amount")),
              currency: String(f.get("currency")),
              period_count:
                kind === "installment" ? Number(f.get("periods")) : null,
              start_month: String(f.get("month")) + "-01",
              day_of_month: Number(f.get("day")),
              merchant: String(f.get("merchant")),
              category_id: String(f.get("category")),
              account_id: String(f.get("account")) || null,
            };
            if (item) {
              await api.command(
                "/spending-schedules/" + item.id,
                "PATCH",
                { ...body, expected_version: item.row_version },
                "修改月度计划",
              );
              refresh();
              notify("计划已更新");
              onClose();
            } else {
              const result = await api.request<Preview>(
                "/spending-schedules/preview",
                { method: "POST", body: JSON.stringify(body) },
              );
              setPreview({ body, result });
            }
          }}
        >
          <Field label="计划名称">
            <input
              name="name"
              required
              defaultValue={saved(
                "name",
                item?.name ||
                  replacement?.merchant ||
                  sourceDraft?.merchant ||
                  "",
              )}
              placeholder="比如：房租、订阅、家电分期"
            />
          </Field>
          <div className="form-grid">
            <Field label="计划类型">
              <select
                value={kind}
                disabled={!!replacement || !!sourceDraft}
                onChange={(e) => setKind(e.target.value)}
              >
                <option value="recurring">每月固定消费</option>
                <option value="installment">分期消费</option>
              </select>
            </Field>
            <Field label="每期金额">
              <input
                name="amount"
                inputMode="decimal"
                required
                defaultValue={saved("amount", item?.amount_per_period || "")}
              />
            </Field>
            <Field label="币种">
              <select
                name="currency"
                defaultValue={saved(
                  "currency",
                  item?.currency ||
                    replacement?.original_currency ||
                    sourceDraft?.currency ||
                    "CNY",
                )}
              >
                {currencies.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </select>
            </Field>
            {kind === "installment" && (
              <Field label="总期数">
                <input
                  name="periods"
                  type="number"
                  min="1"
                  max="1200"
                  required
                  defaultValue={saved(
                    "periods",
                    item?.period_count || sourceDraft?.periods || 12,
                  )}
                />
              </Field>
            )}
            <Field label="开始月份">
              <input
                name="month"
                type="month"
                required
                defaultValue={saved(
                  "month",
                  item?.start_month.slice(0, 7) || today(timezone).slice(0, 7),
                )}
              />
            </Field>
            <Field label="每月几号记账">
              <input
                name="day"
                type="number"
                min="1"
                max="31"
                required
                defaultValue={saved("day", item?.day_of_month || 1)}
              />
            </Field>
          </div>
          <Field label="商户 / 用途">
            <input
              name="merchant"
              required
              defaultValue={saved(
                "merchant",
                item?.merchant ||
                  replacement?.merchant ||
                  sourceDraft?.merchant ||
                  "",
              )}
            />
          </Field>
          <div className="form-grid">
            <Field label="分类">
              <select
                name="category"
                required
                defaultValue={saved(
                  "category",
                  item?.category_id ||
                    replacement?.category_id ||
                    sourceDraft?.category_id ||
                    "",
                )}
              >
                <option value="">请选择</option>
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
            <Field label="账户">
              <select
                name="account"
                defaultValue={saved(
                  "account",
                  item?.account_id ||
                    replacement?.account_id ||
                    sourceDraft?.account_id ||
                    "",
                )}
              >
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
          </div>
        </ActionForm>
      )}
    </Modal>
  );
}
export function Schedules() {
  const { api, refresh, notify } = useApp();
  const [cursor, setCursor] = useState<string | null>(null),
    [edit, setEdit] = useState<Schedule | true | null>(null),
    [history, setHistory] = useState<Schedule | null>(null),
    [transition, setTransition] = useState<{
      item: Schedule;
      action: string;
    } | null>(null),
    [error, setError] = useState<Error | null>(null);
  const list = useResource<Page<Schedule>>(
    "/spending-schedules" + query({ cursor, limit: 30 }),
  );
  return (
    <>
      <Card
        title="月度计划"
        action={
          <Button kind="primary" onClick={() => setEdit(true)}>
            <Plus size={15} />
            新建计划
          </Button>
        }
      >
        <p className="muted">
          记录房租、订阅和分期消费。到期后可能重复的记录会放入待处理。
        </p>
        <ErrorBox error={list.error || error} retry={list.reload} />
        <Button
          onClick={async () => {
            try {
              await api.command(
                "/spending-schedules/materialize",
                "POST",
                {},
                "更新到期计划",
              );
              refresh();
              notify("到期计划已更新");
            } catch (e) {
              setError(e as Error);
            }
          }}
        >
          更新到期记录
        </Button>
        {list.loading ? (
          <Loading />
        ) : list.data?.items.length ? (
          list.data.items.map((s) => (
            <div key={s.id} className="schedule-row">
              <CalendarDays size={22} />
              <div>
                <strong>{s.name}</strong>
                <small>
                  每月 {s.day_of_month} 日 ·{" "}
                  {s.kind === "installment"
                    ? `${s.period_count} 期`
                    : "固定消费"}
                </small>
              </div>
              <b>{money(s.amount_per_period, s.currency)}</b>
              <Tag>
                {(
                  {
                    active: "进行中",
                    paused: "已暂停",
                    cancelled: "已取消",
                    completed: "已完成",
                  } as Record<string, string>
                )[s.status] || s.status}
              </Tag>
              <Button onClick={() => setHistory(s)}>逐期记录</Button>
              {["active", "paused"].includes(s.status) && (
                <Button onClick={() => setEdit(s)}>修改</Button>
              )}
              {["active", "paused"].includes(s.status) && (
                <Button
                  onClick={() =>
                    setTransition({
                      item: s,
                      action: s.status === "paused" ? "resume" : "pause",
                    })
                  }
                >
                  {s.status === "paused" ? "恢复" : "暂停"}
                </Button>
              )}
              {["active", "paused"].includes(s.status) && (
                <Button
                  kind="ghost danger"
                  onClick={() => setTransition({ item: s, action: "cancel" })}
                >
                  结束计划
                </Button>
              )}
            </div>
          ))
        ) : (
          <Empty title="暂无月度计划" />
        )}
        {list.data?.next_cursor && (
          <Button onClick={() => setCursor(list.data!.next_cursor)}>
            下一页计划
          </Button>
        )}
        {cursor && <Button onClick={() => setCursor(null)}>回到第一页</Button>}
      </Card>
      {edit && (
        <ScheduleEditor
          item={edit === true ? undefined : edit}
          onClose={() => setEdit(null)}
        />
      )}{" "}
      {history && (
        <ScheduleHistory item={history} onClose={() => setHistory(null)} />
      )}
      {transition && (
        <Modal
          title={
            transition.action === "pause"
              ? "暂停这个计划？"
              : transition.action === "cancel"
                ? "结束这个计划？"
                : "恢复这个计划？"
          }
          onClose={() => setTransition(null)}
        >
          <p>
            {transition.action === "cancel"
              ? "以后不再新增本计划的支出，已记过的消费会保留。"
              : "暂停期间到期的期数会被跳过。"}
          </p>
          <ActionForm
            label="确认"
            onSave={async (f) => {
              await api.command(
                `/spending-schedules/${transition.item.id}/${transition.action}`,
                "POST",
                {
                  expected_version: transition.item.row_version,
                  reason: String(f.get("reason")) || null,
                },
                "调整月度计划",
              );
              refresh();
              setTransition(null);
            }}
          >
            <Field label="备注（可选）">
              <input name="reason" />
            </Field>
          </ActionForm>
        </Modal>
      )}
    </>
  );
}

function ScheduleHistory({
  item,
  onClose,
}: {
  item: Schedule;
  onClose: () => void;
}) {
  const [cursor, setCursor] = useState(0);
  const detail = useResource<
    Schedule & {
      occurrences: {
        id: string;
        period_no: number;
        due_on: string;
        amount: string;
        currency: string;
        status: string;
      }[];
      next_period_cursor: number | null;
    }
  >(
    `/spending-schedules/${item.id}` +
      query({ after_period: cursor, limit: 50 }),
  );
  const status: Record<string, string> = {
    recorded: "已记账",
    skipped: "已跳过",
    needs_confirmation: "待检查",
    pending: "未到期",
  };
  return (
    <Modal title={item.name + " · 逐期记录"} onClose={onClose}>
      <ErrorBox error={detail.error} retry={detail.reload} />
      {detail.loading ? (
        <Loading />
      ) : detail.data?.occurrences.length ? (
        detail.data.occurrences.map((o) => (
          <div className="list-row" key={o.id}>
            <div>
              <strong>
                第 {o.period_no} 期 · {money(o.amount, o.currency)}
              </strong>
              <small>{day(o.due_on)}</small>
            </div>
            {o.status === "needs_confirmation" ? (
              <a href="#/review" onClick={onClose}>
                去待处理核对
              </a>
            ) : (
              <Tag>{status[o.status] || "待更新"}</Tag>
            )}
          </div>
        ))
      ) : (
        <Empty title="还没有到期记录" />
      )}
      {detail.data?.next_period_cursor && (
        <Button onClick={() => setCursor(detail.data!.next_period_cursor!)}>
          之后的期数
        </Button>
      )}
      {cursor > 0 && <Button onClick={() => setCursor(0)}>回到第一期</Button>}
    </Modal>
  );
}

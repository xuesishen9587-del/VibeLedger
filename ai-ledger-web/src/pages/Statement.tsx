import { useEffect, useRef, useState } from "react";
import {
  FileUp,
  FileText,
  Search,
  ChevronRight,
  Check,
  ArrowLeft,
  LoaderCircle,
  AlertCircle,
} from "lucide-react";
import { useApp, useResource, navigate } from "../lib/context";
import { query } from "../lib/api";
import { ApiError, errorMessage } from "../lib/errors";
import { day, money, kindLabel, currencies } from "../lib/format";
import {
  statementEdit,
  statementSummary,
  lineWarnings,
  reviewReason,
  statementGlobalWarnings,
  globalRecovery,
} from "../lib/statement";
import type {
  Receipt,
  StatementDraft,
  StatementLine,
  Page,
  Transaction,
  Schedule,
  Snapshot,
  Warning,
} from "../types";
import {
  Heading,
  Button,
  Card,
  Field,
  ActionForm,
  ErrorBox,
  Empty,
  Loading,
  Tag,
  Modal,
} from "../components/ui";

function StatementUpload() {
  const { api, accounts, notify } = useApp();
  const keyName = "vl-upload:" + api.subject;
  const [pending, setPending] = useState<{
    key: string;
    account: string;
  } | null>(() => {
    try {
      return JSON.parse(sessionStorage.getItem(keyName) || "null");
    } catch {
      return null;
    }
  });
  const [error, setError] = useState<Error | null>(null),
    [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const [recognizing, setRecognizing] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!recognizing) return;
    const started = Date.now();
    setElapsed(0);
    const timer = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - started) / 1000)),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [recognizing]);
  const eligible = accounts.filter(
    (a) => a.status === "active" && a.statement_import_enabled,
  );
  const remember = (value: { key: string; account: string } | null) => {
    if (value) sessionStorage.setItem(keyName, JSON.stringify(value));
    else sessionStorage.removeItem(keyName);
    setPending(value);
  };
  const receive = (r: Receipt) => {
    if (r.status === "processing") {
      notify("账单还在识别，稍后可以再查询。");
      return;
    }
    remember(null);
    if (r.request_id) navigate("/statement/" + r.request_id);
  };
  async function recover(cancel = false) {
    setBusy(true);
    setError(null);
    try {
      receive(
        await api.request<Receipt>(
          `/ingestion-requests/by-key/${pending!.key}${cancel ? "/cancel" : ""}`,
          { method: cancel ? "POST" : "GET" },
        ),
      );
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Card className="upload-card">
      <div className="upload-icon">
        <FileUp size={28} />
      </div>
      <h2>上传 PDF 账单</h2>
      <p>上传 PDF，先看识别结果，再一次确认导入。</p>
      {!pending && <ErrorBox error={error} />}
      {recognizing ? (
        <div className="recognition-progress" aria-busy="true">
          <div role="status">
            <strong>正在读取并识别账单，请稍候</strong>
            <p>
              {elapsed >= 30
                ? "识别仍在进行，较长账单需要更多时间，请不要重复上传同一份 PDF。"
                : "正在处理 PDF，完成后会自动显示核对结果。"}
            </p>
          </div>
          <progress aria-label="账单识别进行中" />
          <p className="muted">已等待 {elapsed} 秒</p>
          <Button disabled>
            <LoaderCircle className="spin" size={17} />
            正在识别…
          </Button>
        </div>
      ) : pending ? (
        <div className="notice warning">
          <strong>上次上传的结果还未确认</strong>
          <p>可以先查询结果，或取消本次上传后重新选择文件。</p>
          <div className="actions">
            <Button
              disabled={busy}
              onClick={() => {
                void recover();
              }}
            >
              查询识别结果
            </Button>
            <Button
              disabled={busy}
              onClick={() => {
                void recover(true);
              }}
            >
              取消这次上传
            </Button>
          </div>
          <ErrorBox error={error} />
        </div>
      ) : eligible.length ? (
        <ActionForm
          label="开始识别账单"
          onSave={async (form) => {
            const file = form.get("file") as File;
            if (
              !file?.size ||
              file.size > 20 * 1024 * 1024 ||
              !file.name.toLowerCase().endsWith(".pdf")
            )
              throw new ApiError("STATEMENT_LIMIT_EXCEEDED");
            const p = {
              key: crypto.randomUUID(),
              account: String(form.get("account")),
            };
            setBusy(true);
            setRecognizing(true);
            setError(null);
            remember(p);
            const data = new FormData();
            data.set("file", file);
            if (form.get("password"))
              data.set("password", String(form.get("password")));
            try {
              receive(
                await api.request<Receipt>(
                  `/accounts/${p.account}/statement-imports`,
                  {
                    method: "POST",
                    headers: { "Idempotency-Key": p.key },
                    body: data,
                  },
                ),
              );
            } catch (e) {
              setError(e as Error);
              if (
                e instanceof ApiError &&
                e.code === "STATEMENT_ALREADY_UPLOADED" &&
                typeof e.details.request_id === "string"
              ) {
                remember(null);
                navigate("/statement/" + e.details.request_id);
                return;
              }
              if (
                e instanceof ApiError &&
                e.status >= 400 &&
                e.status < 500 &&
                ![401, 408, 429].includes(e.status)
              )
                remember(null);
              throw e;
            } finally {
              setBusy(false);
              setRecognizing(false);
              const password = document.querySelector<HTMLInputElement>(
                'input[name="password"]',
              );
              if (password) password.value = "";
              if (fileRef.current) fileRef.current.value = "";
            }
          }}
        >
          <Field label="这是哪个账户的账单？">
            <select name="account" required>
              {eligible.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} · {a.currency}
                </option>
              ))}
            </select>
          </Field>
          <label className="drop-zone">
            <FileText size={25} />
            <strong>选择 PDF 账单</strong>
            <span>最多 20 MiB · 50 页</span>
            <input
              ref={fileRef}
              name="file"
              aria-label="PDF 账单"
              type="file"
              accept="application/pdf,.pdf"
              required
            />
          </label>
          <Field label="PDF 密码（如有）">
            <input
              type="password"
              name="password"
              autoComplete="off"
              maxLength={200}
              placeholder="仅用于打开这份账单"
            />
          </Field>
        </ActionForm>
      ) : (
        <Empty title="先选择一个可以导入账单的账户">
          <a href="#/settings">去账户设置开启账单导入</a>
        </Empty>
      )}
    </Card>
  );
}
function StatementDetail({
  row,
  onChange,
  onClose,
  onConfirm,
  warnings,
  busy,
  error: saveError,
}: {
  row: StatementLine;
  onChange: (update: Partial<StatementLine>) => void;
  onClose: () => void;
  onConfirm: () => void;
  warnings: Warning[];
  busy: boolean;
  error: Error | null;
}) {
  const { api, categories } = useApp();
  const blocked = warnings.some(
    (w) => !["STATEMENT_LINE_UNCERTAIN", "POSSIBLE_DUPLICATE"].includes(w.code),
  );
  const [targets, setTargets] = useState<Transaction[]>([]),
    [error, setError] = useState<Error | null>(null);
  const schedules = useResource<Page<Schedule>>(
    row.action === "use_schedule_period"
      ? "/spending-schedules?limit=200"
      : null,
  );
  const [search, setSearch] = useState("");
  const candidates = useResource<Page<Transaction>>(
    row.action === "link_existing"
      ? "/transactions" +
          query({ merchant: search || row.merchant, cursor: null, limit: 30 })
      : null,
  );
  useEffect(() => {
    let active = true;
    Promise.all(
      row.duplicate_ids.map((id) =>
        api.request<Transaction>("/transactions/" + id),
      ),
    ).then(
      (items) => {
        if (active) setTargets(items);
      },
      (e) => {
        if (active) setError(e);
      },
    );
    return () => {
      active = false;
    };
  }, [api, row.row_id, row.duplicate_ids]);
  const choices = [
    ...new Map(
      [...targets, ...(candidates.data?.items || [])].map((t) => [t.id, t]),
    ).values(),
  ];
  return (
    <Modal
      title={row.merchant || `第 ${row.row_no} 笔交易`}
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <ErrorBox error={saveError} />
      {warnings.length > 0 && (
        <div className="notice warning" role="status">
          <strong>
            {blocked ? "请修改以下问题后重新检查" : "请核对以下疑点"}
          </strong>
          <ul>
            {warnings.map((w, i) => (
              <li key={i}>{reviewReason(w)}</li>
            ))}
          </ul>
        </div>
      )}
      <fieldset disabled={busy}>
        <p className="muted">
          {day(row.occurred_on)} ·{" "}
          {money(row.original_amount, row.original_currency || "CNY")}
        </p>
        <Field label="怎样处理这笔？">
          <select
            value={row.action}
            onChange={(e) =>
              onChange({
                action: e.target.value as StatementLine["action"],
                reason:
                  e.target.value === "skip" ? "不计入本次支出" : row.reason,
              })
            }
          >
            <option value="create">记为一笔独立消费 / 退款</option>
            <option value="link_existing">已经记过，关联已有记录</option>
            <option value="skip">跳过，不计入支出</option>
            <option value="use_schedule_period">对应月度计划的某一期</option>
          </select>
        </Field>
        {row.action === "create" || row.action === "use_schedule_period" ? (
          <div className="form-grid">
            <Field label="交易日期">
              <input
                type="date"
                value={row.occurred_on || ""}
                onChange={(e) => onChange({ occurred_on: e.target.value })}
              />
            </Field>
            <Field label="交易金额">
              <input
                inputMode="decimal"
                value={row.original_amount || ""}
                onChange={(e) => onChange({ original_amount: e.target.value })}
              />
            </Field>
            <Field label="交易币种">
              <select
                value={row.original_currency || ""}
                onChange={(e) =>
                  onChange({ original_currency: e.target.value })
                }
              >
                <option value="">请选择</option>
                {currencies.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </select>
            </Field>
            <Field label="交易性质">
              <select
                value={row.transaction_type || ""}
                onChange={(e) =>
                  onChange({
                    transaction_type: e.target.value as "expense" | "refund",
                  })
                }
              >
                <option value="">请选择</option>
                <option value="expense">消费</option>
                <option value="refund">退款</option>
              </select>
            </Field>
            <Field label="支出分类">
              <select
                value={row.category_id || ""}
                onChange={(e) => onChange({ category_id: e.target.value })}
              >
                <option value="">请选择</option>
                {categories
                  .filter(
                    (c) =>
                      c.status === "active" && c.category_type === "expense",
                  )
                  .map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
              </select>
            </Field>
          </div>
        ) : null}
        {row.duplicate_ids.length > 0 && (
          <div className="notice warning">
            发现可能重复的记录。选择独立记录表示这是另一笔真实交易。
          </div>
        )}
        {row.action === "link_existing" && (
          <>
            <Field label="查找已有记录">
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="搜索商户"
              />
            </Field>
            <ErrorBox error={error || candidates.error} />
            <Field label="选择已经记过的那一笔">
              <select
                value={row.transaction_id || ""}
                onChange={(e) => {
                  const t = choices.find((t) => t.id === e.target.value);
                  onChange({
                    transaction_id: t?.id || null,
                    expected_transaction_version: t?.row_version ?? null,
                  });
                }}
              >
                <option value="">请选择</option>
                {choices.map((t) => (
                  <option key={t.id} value={t.id}>
                    {day(t.occurred_on)} ·{" "}
                    {t.merchant || kindLabel[t.transaction_type]} ·{" "}
                    {money(t.original_amount, t.original_currency)}
                  </option>
                ))}
              </select>
            </Field>
          </>
        )}
        {row.action === "use_schedule_period" && (
          <>
            <ErrorBox error={schedules.error} />
            <Field label="月度计划">
              <select
                value={row.schedule_id || ""}
                onChange={(e) => {
                  const s = schedules.data?.items.find(
                    (s) => s.id === e.target.value,
                  );
                  onChange({
                    schedule_id: s?.id || null,
                    expected_schedule_version: s?.row_version ?? null,
                    period_no: row.period_no || 1,
                  });
                }}
              >
                <option value="">请选择</option>
                {schedules.data?.items.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </Field>
            {schedules.data?.next_cursor && (
              <p className="notice">
                只列出前 200 个计划，请先在月度计划中定位目标。
              </p>
            )}
            <Field label="第几期">
              <input
                type="number"
                min="1"
                value={row.period_no || 1}
                onChange={(e) =>
                  onChange({ period_no: Number(e.target.value) })
                }
              />
            </Field>
          </>
        )}
        <Field label="备注">
          <input
            value={row.remarks || ""}
            onChange={(e) => onChange({ remarks: e.target.value || null })}
            maxLength={2000}
          />
        </Field>
        {row.action === "skip" && (
          <Field label="跳过原因">
            <input
              value={row.reason || ""}
              onChange={(e) => onChange({ reason: e.target.value })}
            />
          </Field>
        )}
        <p className="muted small">
          {blocked
            ? "核对不能代替修改。保存后会重新校验，仍有问题会在此显示。"
            : "核对金额、日期和交易性质后，确认这一笔。其他交易的疑点会继续保留。"}
        </p>
        <div className="form-footer">
          <Button
            kind="primary"
            disabled={
              (row.action === "link_existing" && !row.transaction_id) ||
              (row.action === "use_schedule_period" && !row.schedule_id)
            }
            onClick={onConfirm}
          >
            <Check size={16} />
            {busy
              ? "正在保存并检查…"
              : blocked
                ? "保存修改并重新检查"
                : row.action === "create" && row.duplicate_ids.length
                  ? "确认是另一笔交易"
                  : "这一笔已核对"}
          </Button>
        </div>
      </fieldset>
    </Modal>
  );
}
function ClosingBalance({
  draft,
  setDraft,
}: {
  draft: StatementDraft;
  setDraft: (d: StatementDraft) => void;
}) {
  const { accounts } = useApp();
  const b = draft.balance!;
  const account = accounts.find((a) => a.id === draft.account_id);
  const [cursor, setCursor] = useState<string | null>(null);
  const history = useResource<Page<Snapshot>>(
    b.selected
      ? `/accounts/${draft.account_id}/snapshots` + query({ limit: 50, cursor })
      : null,
  );
  const [all, setAll] = useState<Snapshot[]>([]);
  useEffect(() => {
    if (history.data)
      setAll((prior) => [
        ...new Map(
          [...prior, ...history.data!.items].map((s) => [s.id, s]),
        ).values(),
      ]);
  }, [history.data]);
  const set = (fields: Partial<typeof b>) =>
    setDraft({
      ...draft,
      balance: {
        ...b,
        ...fields,
        expected_account_version:
          account?.row_version ?? b.expected_account_version,
        expected_latest_snapshot_id: account?.latest_snapshot?.id ?? null,
      },
    });
  return (
    <details className="balance-option">
      <summary>
        同时更新账单余额 <Tag>{b.selected ? "已选择" : "仅导入支出"}</Tag>
      </summary>
      <p>余额与消费合计分开记录。信用卡的本期应还款不等于总欠款。</p>
      <label className="check">
        <input
          type="checkbox"
          checked={b.selected}
          onChange={(e) => set({ selected: e.target.checked })}
        />
        同时保存这个账户的余额
      </label>
      {b.selected && (
        <>
          <div className="form-grid">
            <Field label="账户余额（欠款填负数）">
              <input
                value={b.balance || ""}
                onChange={(e) => set({ balance: e.target.value })}
              />
            </Field>
            <Field label="余额对应的时间">
              <input
                aria-label="余额时间"
                value={b.as_of || ""}
                onChange={(e) => set({ as_of: e.target.value })}
              />
            </Field>
          </div>
          <Field label="已有相同日期和金额的记录？">
            <select
              value={b.reuse_snapshot_id || ""}
              onChange={(e) =>
                set({ reuse_snapshot_id: e.target.value || null })
              }
            >
              <option value="">新增这次余额</option>
              {b.reuse_snapshot_id &&
                !all.some((s) => s.id === b.reuse_snapshot_id) && (
                  <option value={b.reuse_snapshot_id}>
                    先前选择的余额（待核验）
                  </option>
                )}
              {all.map((s) => (
                <option key={s.id} value={s.id}>
                  {day(s.as_of)} · {money(s.balance, s.currency)}
                </option>
              ))}
            </select>
          </Field>
          <ErrorBox error={history.error} />
          {history.data?.next_cursor && (
            <Button onClick={() => setCursor(history.data!.next_cursor)}>
              更早的余额
            </Button>
          )}
        </>
      )}
    </details>
  );
}
export function StatementWorkspace({
  receipt,
  onReceipt,
}: {
  receipt: Receipt;
  onReceipt: (r: Receipt) => void;
}) {
  const { api, accounts, categories, refresh, notify } = useApp();
  const storageKey = "vl-statement:" + api.subject + ":" + receipt.request_id;
  const [draft, setDraft] = useState<StatementDraft>(() =>
    structuredClone(receipt.draft),
  );
  const [confirmed, setConfirmed] = useState(new Set<string>()),
    [confirmedCategories, setConfirmedCategories] = useState(new Set<string>());
  const [filter, setFilter] = useState("all"),
    [search, setSearch] = useState(""),
    [selected, setSelected] = useState<string | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<Error | null>(null),
    [restored, setRestored] = useState(false);
  const [discard, setDiscard] = useState(false);
  const inputVersion = useRef(receipt.row_version);
  useEffect(() => {
    try {
      const local = JSON.parse(sessionStorage.getItem(storageKey) || "null");
      if (local && local.version === receipt.row_version) {
        setDraft(local.draft);
        setConfirmed(new Set(local.confirmed));
        setConfirmedCategories(new Set(local.categories));
        setRestored(true);
      } else if (local) {
        setError(
          new Error("这份账单已更新，已载入最新结果。此前本地修改未自动覆盖。"),
        );
      }
    } catch {
      setError(new ApiError("STORAGE_UNAVAILABLE"));
    }
  }, [storageKey]);
  useEffect(() => {
    if (inputVersion.current !== receipt.row_version) {
      inputVersion.current = receipt.row_version;
      setDraft(structuredClone(receipt.draft));
      setConfirmed(new Set());
      setConfirmedCategories(new Set());
      setRestored(false);
    }
  }, [receipt]);
  useEffect(() => {
    if (inputVersion.current !== receipt.row_version) return;
    try {
      sessionStorage.setItem(
        storageKey,
        JSON.stringify({
          version: receipt.row_version,
          draft,
          confirmed: [...confirmed],
          categories: [...confirmedCategories],
        }),
      );
    } catch {
      setError(new ApiError("STORAGE_UNAVAILABLE"));
    }
  }, [draft, confirmed, confirmedCategories, storageKey, receipt.row_version]);
  const update = (id: string, change: Partial<StatementLine>) => {
    setDraft((d) => ({
      ...d,
      lines: d.lines.map((r) => (r.row_id === id ? { ...r, ...change } : r)),
    }));
    if ("category_id" in change)
      setConfirmedCategories((s) => new Set([...s, id]));
  };
  const summary = statementSummary(draft, confirmed);
  const globalWarnings = statementGlobalWarnings(draft);
  const needs = (row: StatementLine) =>
    (row.action !== "skip" &&
      row.requires_review &&
      !confirmed.has(row.row_id)) ||
    lineWarnings(row, draft.warnings, confirmed).length > 0;
  const visible = draft.lines.filter(
    (r) =>
      (filter === "all" || needs(r)) &&
      (!search ||
        (r.merchant || "").toLowerCase().includes(search.toLowerCase())),
  );
  const row = draft.lines.find((r) => r.row_id === selected);
  const account = accounts.find((a) => a.id === draft.account_id);
  const hasSignedAmounts = draft.lines.some(
    (r) => r.action !== "skip" && /^-\d/.test(r.original_amount || ""),
  );
  async function repairAmounts() {
    setBusy(true);
    setError(null);
    try {
      const body = statementEdit(
        receipt.draft,
        draft,
        receipt.row_version,
        confirmed,
        confirmedCategories,
      );
      const result = await api.request<Receipt>(
        `/ingestion-requests/${receipt.request_id}/draft`,
        {
          method: "PATCH",
          body: JSON.stringify({ ...body, normalize_display_amounts: true }),
        },
      );
      onReceipt(result);
      notify("已统一原始金额的正负号，其余待检查项继续保留");
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }
  const received = (r: Receipt) => {
    onReceipt(r);
    if (r.status === "committed") {
      sessionStorage.removeItem(storageKey);
      notify("账单已导入");
      refresh();
    }
  };
  async function reviewRow(row: StatementLine) {
    setBusy(true);
    setError(null);
    try {
      const result = await api.request<Receipt>(
        `/ingestion-requests/${receipt.request_id}/draft`,
        {
          method: "PATCH",
          body: JSON.stringify(
            statementEdit(
              receipt.draft,
              draft,
              receipt.row_version,
              new Set([...confirmed, row.row_id]),
              confirmedCategories,
            ),
          ),
        },
      );
      onReceipt(result);
      if (!result.draft.warnings.some((w) => w.row_id === row.row_id))
        setSelected(null);
    } catch (e) {
      setError(e as Error);
      // Recover an uncertain PATCH without discarding local edits or acknowledging another row.
      if (e instanceof ApiError && (!e.status || e.status === 409)) {
        try {
          const current = await api.request<Receipt>(
            `/ingestion-requests/${receipt.request_id}`,
          );
          if (current.row_version !== receipt.row_version) onReceipt(current);
        } catch {
          /* Keep edits available for retry. */
        }
      }
    } finally {
      setBusy(false);
    }
  }
  function showBlockers(result: Receipt) {
    const counts = statementSummary(result.draft);
    setSearch("");
    setFilter(counts.review ? "review" : "all");
    setError(
      new Error(
        counts.globalReview
          ? "整份账单仍有阻塞问题，请按“整单阻塞”中的说明处理。"
          : `还有 ${counts.review} 笔交易需要检查，请打开标出的记录修改。`,
      ),
    );
  }
  async function save(recheckOnly = false) {
    setBusy(true);
    setError(null);
    const base = "/ingestion-requests/" + receipt.request_id;
    try {
      let result = receipt;
      const body = statementEdit(
        receipt.draft,
        draft,
        receipt.row_version,
        confirmed,
        confirmedCategories,
      );
      const changed =
        JSON.stringify(receipt.draft) !== JSON.stringify(draft) ||
        confirmed.size ||
        confirmedCategories.size;
      if (changed || receipt.warnings?.length || recheckOnly) {
        result = await api.request<Receipt>(base + "/draft", {
          method: "PATCH",
          body: JSON.stringify(body),
        });
        onReceipt(result);
      }
      if (result.warnings?.length) {
        showBlockers(result);
        return;
      }
      if (recheckOnly) {
        notify("已重新检查，请核对结果后确认导入整份账单。");
        return;
      }
      result = await api.request<Receipt>(base + "/confirm", {
        method: "POST",
        body: JSON.stringify({ expected_version: result.row_version }),
      });
      received(result);
      if (result.status === "needs_confirmation") showBlockers(result);
    } catch (e) {
      setError(e as Error);
      if (e instanceof ApiError && (!e.status || e.status === 409)) {
        try {
          const current = await api.request<Receipt>(base);
          if (current.status === "committed") received(current);
          else if (current.row_version !== receipt.row_version) {
            onReceipt(current);
            setError(new Error("已找回服务器上的最新结果，请核对后继续导入。"));
          }
        } catch {
          /* retain local edits for recovery */
        }
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="statement-meta">
        <span className="statement-account">
          <FileText size={20} />
          <strong>{account?.name || "账户账单"}</strong>
          <Tag>{account?.currency}</Tag>
        </span>
        <div className="date-range">
          <input
            type="date"
            aria-label="账单开始日期"
            value={draft.period_start || ""}
            onChange={(e) =>
              setDraft({ ...draft, period_start: e.target.value })
            }
          />
          <span>至</span>
          <input
            type="date"
            aria-label="账单结束日期"
            value={draft.period_end || ""}
            onChange={(e) => setDraft({ ...draft, period_end: e.target.value })}
          />
        </div>
      </div>
      <div className="statement-summary">
        <div>
          <strong>{summary.purchases}</strong>
          <span>笔消费</span>
        </div>
        <div>
          <strong>{summary.refunds}</strong>
          <span>笔退款</span>
        </div>
        <div>
          <strong>{summary.repayments}</strong>
          <span>笔还款 · 不计支出</span>
        </div>
        <div className="statement-total">
          <span>本次新增净支出</span>
          {summary.totals.map((t) => (
            <strong key={t.currency}>
              {money(t.amount, t.currency)} <small>{t.currency}</small>
            </strong>
          ))}
        </div>
        <div className={summary.review ? "review-number" : ""}>
          <strong>{summary.review}</strong>
          <span>笔待检查</span>
        </div>
        {summary.globalReview > 0 && (
          <div className="review-number">
            <strong>{summary.globalReview}</strong>
            <span>项整单阻塞</span>
          </div>
        )}
      </div>
      {!draft.identity_ok && (
        <label className="notice check">
          <input
            type="checkbox"
            checked={draft.confirm_account_identity}
            onChange={(e) =>
              setDraft({ ...draft, confirm_account_identity: e.target.checked })
            }
          />
          我核对过，这份账单属于「{account?.name}」
        </label>
      )}
      {draft.partial && (
        <label className="notice warning check">
          <input
            type="checkbox"
            checked={draft.acknowledge_partial}
            onChange={(e) =>
              setDraft({ ...draft, acknowledge_partial: e.target.checked })
            }
          />
          有页面可能没读完整，我已核对并接受只导入本次识别的内容
        </label>
      )}
      {restored && <p className="small muted">已恢复上次尚未提交的修改。</p>}
      {hasSignedAmounts && (
        <div className="notice warning">
          <p>
            这份旧草稿保留了银行显示的负号。可以统一消费和退款的金额写法，已手动改过的金额会保留。
          </p>
          <Button disabled={busy} onClick={() => void repairAmounts()}>
            统一原始金额正负号
          </Button>
        </div>
      )}
      <ErrorBox error={error} />
      {globalWarnings.length > 0 && (
        <section className="notice warning" role="alert" aria-label="整单阻塞">
          <h3>整单阻塞：这份账单尚未导入</h3>
          <p>
            这些问题影响整份账单，不计入逐笔待检查数量。未保存任何一笔交易。
          </p>
          <ul>
            {globalWarnings.map((w, i) => (
              <li key={i}>
                <strong>{errorMessage(w.code, w.message)}</strong>
                {w.message && <p>{w.message}</p>}
                <p>{globalRecovery(w)}</p>
              </li>
            ))}
          </ul>
          <Button disabled={busy} onClick={() => void save(true)}>
            保存修改并重新检查整份账单
          </Button>
        </section>
      )}
      <section className="card statement-workspace">
        <div className="filters">
          <div className="tabs">
            <button
              className={filter === "all" ? "selected" : ""}
              onClick={() => setFilter("all")}
            >
              全部 {draft.lines.length} 笔
            </button>
            <button
              className={filter === "review" ? "selected" : ""}
              onClick={() => setFilter("review")}
            >
              只看待检查 {summary.review}
            </button>
          </div>
          <label className="search">
            <Search size={15} />
            <input
              aria-label="搜索账单商户"
              placeholder="搜索商户"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
        </div>
        <fieldset disabled={busy}>
          <div className="table-wrap statement-table-wrap">
            <table className="ledger-table statement-table">
              <thead>
                <tr>
                  <th className="row-number">#</th>
                  <th>日期</th>
                  <th>商户 / 用途</th>
                  <th>性质</th>
                  <th>分类</th>
                  <th className="amount">金额</th>
                  <th>币种</th>
                  <th>处理</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((r) => (
                  <tr
                    key={r.row_id}
                    data-testid="statement-row"
                    className={
                      (needs(r) ? "needs-review " : "") +
                      (r.action === "skip" ? "skipped" : "")
                    }
                  >
                    <td className="row-number">{r.row_no}</td>
                    <td>
                      <input
                        aria-label={`第 ${r.row_no} 笔日期`}
                        type="date"
                        value={r.occurred_on || ""}
                        onChange={(e) =>
                          update(r.row_id, { occurred_on: e.target.value })
                        }
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`第 ${r.row_no} 笔商户`}
                        value={r.merchant || ""}
                        placeholder="未识别商户"
                        onChange={(e) =>
                          update(r.row_id, { merchant: e.target.value })
                        }
                      />
                    </td>
                    <td>
                      {r.action === "skip" ? (
                        <span>{kindLabel[r.kind || ""] || "非支出"}</span>
                      ) : (
                        <select
                          aria-label={`第 ${r.row_no} 笔性质`}
                          value={r.transaction_type || ""}
                          onChange={(e) =>
                            update(r.row_id, {
                              transaction_type: e.target.value as
                                "expense" | "refund",
                            })
                          }
                        >
                          <option value="">待确认</option>
                          <option value="expense">消费</option>
                          <option value="refund">退款</option>
                        </select>
                      )}
                    </td>
                    <td>
                      <select
                        aria-label={`第 ${r.row_no} 笔分类`}
                        value={r.category_id || ""}
                        onChange={(e) =>
                          update(r.row_id, { category_id: e.target.value })
                        }
                      >
                        <option value="">待分类</option>
                        {categories
                          .filter(
                            (c) =>
                              c.category_type === "expense" &&
                              c.status === "active",
                          )
                          .map((c) => (
                            <option key={c.id} value={c.id}>
                              {c.is_fallback && r.category_uncertain
                                ? "待分类"
                                : c.name}
                            </option>
                          ))}
                      </select>
                    </td>
                    <td>
                      <input
                        className="amount"
                        aria-label={`第 ${r.row_no} 笔金额`}
                        inputMode="decimal"
                        value={r.original_amount || ""}
                        onChange={(e) =>
                          update(r.row_id, { original_amount: e.target.value })
                        }
                      />
                    </td>
                    <td>
                      <select
                        aria-label={`第 ${r.row_no} 笔币种`}
                        value={r.original_currency || ""}
                        onChange={(e) =>
                          update(r.row_id, {
                            original_currency: e.target.value,
                          })
                        }
                      >
                        <option value="">—</option>
                        {currencies.map((c) => (
                          <option key={c}>{c}</option>
                        ))}
                      </select>
                    </td>
                    <td>
                      <button
                        aria-label={needs(r) ? "检查" : undefined}
                        className={
                          "row-status " + (needs(r) ? "attention" : "")
                        }
                        title={lineWarnings(r, draft.warnings, confirmed)
                          .map(reviewReason)
                          .join("；")}
                        onClick={() => setSelected(r.row_id)}
                      >
                        {needs(r) ? (
                          <AlertCircle size={13} />
                        ) : (
                          <Check size={13} />
                        )}{" "}
                        {needs(r)
                          ? "检查"
                          : r.action === "skip"
                            ? "跳过"
                            : r.action === "link_existing"
                              ? "已记过"
                              : r.action === "use_schedule_period"
                                ? "月度计划"
                                : "就绪"}
                        <ChevronRight size={12} />
                      </button>
                      {lineWarnings(r, draft.warnings, confirmed)
                        .filter(
                          (w) =>
                            ![
                              "STATEMENT_LINE_UNCERTAIN",
                              "POSSIBLE_DUPLICATE",
                            ].includes(w.code),
                        )
                        .map((w, i) => (
                          <p className="row-review-reason" key={i}>
                            {reviewReason(w)}
                          </p>
                        ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!visible.length && (
              <Empty
                title={
                  filter === "review"
                    ? "这份账单没有待检查的交易"
                    : "没有找到这笔交易"
                }
              />
            )}
          </div>
        </fieldset>
        <div className="table-note">
          正常记录可以直接导入。点击金额或分类就能修改；只需逐笔检查标黄的记录。
        </div>
      </section>
      {draft.balance && <ClosingBalance draft={draft} setDraft={setDraft} />}
      <div className="statement-footer">
        <span>
          <strong>{summary.purchases + summary.refunds} 笔新增</strong> ·{" "}
          {summary.linked} 笔已记过 · {summary.skipped} 笔跳过
        </span>
        <div className="actions">
          <Button disabled={busy} kind="ghost" onClick={() => setDiscard(true)}>
            放弃账单
          </Button>
          <Button
            disabled={
              busy ||
              (!draft.identity_ok && !draft.confirm_account_identity) ||
              (draft.partial && !draft.acknowledge_partial)
            }
            kind="primary"
            onClick={() => {
              void save();
            }}
          >
            {busy ? (
              <LoaderCircle className="spin" size={17} />
            ) : (
              <Check size={17} />
            )}{" "}
            {busy ? "正在保存…" : "确认导入整份账单"}
          </Button>
        </div>
      </div>
      {row && (
        <StatementDetail
          row={row}
          warnings={lineWarnings(row, draft.warnings, confirmed)}
          busy={busy}
          error={error}
          onChange={(change) => update(row.row_id, change)}
          onClose={() => setSelected(null)}
          onConfirm={() => {
            void reviewRow(row);
          }}
        />
      )}
      {discard && (
        <Modal title="放弃这份账单？" onClose={() => setDiscard(false)}>
          <p>这份尚未保存的账单将被取消，不会新增消费或改变余额。</p>
          <ActionForm
            label="确认放弃"
            onSave={async () => {
              const r = await api.request<Receipt>(
                "/ingestion-requests/" + receipt.request_id + "/reject",
                {
                  method: "POST",
                  body: JSON.stringify({
                    expected_version: receipt.row_version,
                  }),
                },
              );
              sessionStorage.removeItem(storageKey);
              received(r);
              refresh();
            }}
          />
        </Modal>
      )}
    </>
  );
}
export function StatementPage({ identity }: { identity?: string }) {
  const { accounts } = useApp();
  const data = useResource<Receipt>(
    identity ? "/ingestion-requests/" + identity : null,
  );
  const list = useResource<Page<Receipt>>(
    !identity ? "/ingestion-requests?request_kind=statement&limit=50" : null,
  );
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  useEffect(() => {
    setReceipt(data.data || null);
  }, [data.data, identity]);
  return (
    <>
      <Heading
        title={identity ? "核对账单" : "导入账单"}
        actions={
          identity ? (
            <Button onClick={() => navigate("/statement")}>
              <ArrowLeft size={16} />
              返回账单
            </Button>
          ) : undefined
        }
      >
        {identity
          ? "核对标出的疑问，确认后导入整份账单。"
          : "上传 PDF，核对后批量导入交易。"}
      </Heading>
      {!identity ? (
        <>
          <StatementUpload />
          <Card title="等待导入的账单">
            {list.loading ? (
              <Loading />
            ) : list.error ? (
              <ErrorBox error={list.error} retry={list.reload} />
            ) : list.data?.items.length ? (
              list.data.items.map((r) => (
                <a
                  className="inbox-row"
                  href={"#/statement/" + r.request_id}
                  key={r.request_id}
                >
                  <FileText />
                  <div>
                    <strong>
                      {accounts.find((a) => a.id === r.draft.account_id)
                        ?.name || "账户账单"}
                    </strong>
                    <small>
                      {day(r.draft.period_start)} — {day(r.draft.period_end)} ·{" "}
                      {r.draft.lines.length} 笔
                    </small>
                  </div>
                  <ChevronRight size={18} />
                </a>
              ))
            ) : (
              <Empty title="没有等待导入的账单" />
            )}
            {list.data?.next_cursor && (
              <a href="#/review">更多账单请到待处理查看</a>
            )}
          </Card>
        </>
      ) : data.error ? (
        <ErrorBox error={data.error} retry={data.reload} />
      ) : !receipt ? (
        <Loading />
      ) : receipt.status === "needs_confirmation" ? (
        <StatementWorkspace
          key={identity}
          receipt={receipt}
          onReceipt={setReceipt}
        />
      ) : (
        <Card className="success-card">
          <Check size={36} />
          <h2>
            {receipt.status === "committed"
              ? "账单已导入"
              : receipt.status === "processing"
                ? "还在识别账单…"
                : receipt.status === "rejected"
                  ? "这份账单已取消"
                  : "这次账单未完成"}
          </h2>
          {receipt.counts && (
            <p>
              新增 {receipt.counts.create} 笔 · 关联 {receipt.counts.link} 笔 ·
              跳过 {receipt.counts.skip} 笔
              {receipt.snapshots?.length
                ? ` · 更新 ${receipt.snapshots.length} 个余额`
                : ""}
            </p>
          )}
          <Button
            kind="primary"
            onClick={() =>
              receipt.status === "processing"
                ? data.reload()
                : navigate("/spending")
            }
          >
            {receipt.status === "processing" ? "查询结果" : "查看日常支出"}
          </Button>
        </Card>
      )}
    </>
  );
}

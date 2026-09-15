import { useState } from "react";
import { Wallet, Plus, ImageUp, History, ArrowUpRight } from "lucide-react";
import { useApp, useResource, navigate } from "../lib/context";
import { query } from "../lib/api";
import { ApiError } from "../lib/errors";
import { money, day, today, accountLabel, riskLabel } from "../lib/format";
import type {
  Wealth,
  Account,
  Investment,
  Page,
  Snapshot,
  Receipt,
} from "../types";
import {
  Heading,
  Button,
  Card,
  Tag,
  Field,
  ActionForm,
  Modal,
  ErrorBox,
  Empty,
  Loading,
} from "../components/ui";
import { WealthStats, WealthHistory } from "../components/Charts";
export function InvestmentEditor({
  item,
  onClose,
}: {
  item: Investment;
  onClose: () => void;
}) {
  const { api, notify, refresh } = useApp();
  return (
    <Modal
      title={item.account_name + " · 这段时间的投入与取出"}
      onClose={onClose}
    >
      <p>
        {day(item.period_start)} — {day(item.period_end)}
      </p>
      <p>
        期初 {money(item.opening_value, item.currency)} → 期末{" "}
        {money(item.closing_value, item.currency)}
      </p>
      <ActionForm
        label="确认这段时间的金额"
        onSave={async (f) => {
          await api.command(
            "/investment-period-inputs",
            "PUT",
            {
              opening_snapshot_id: item.opening_snapshot_id,
              closing_snapshot_id: item.closing_snapshot_id,
              contributions_amount: String(f.get("in")),
              withdrawals_amount: String(f.get("out")),
              expected_version: item.input_version,
              notes: String(f.get("notes")) || null,
            },
            "确认投资投入与取出",
          );
          notify("已按确认的投入与取出重新计算收益");
          refresh();
          onClose();
        }}
      >
        <div className="form-grid">
          <Field label={"额外投入 · " + item.currency}>
            <input
              name="in"
              inputMode="decimal"
              defaultValue={item.effective_contributions}
              required
            />
          </Field>
          <Field label={"取出金额 · " + item.currency}>
            <input
              name="out"
              inputMode="decimal"
              defaultValue={item.effective_withdrawals}
              required
            />
          </Field>
        </div>
        <p className="notice subtle">
          没有额外转入或取出，就填 0。账户内部买卖不需要填在这里。
        </p>
        <Field label="备注（可选）">
          <input name="notes" />
        </Field>
        <label className="check">
          <input type="checkbox" required />
          我已核对这段时间的全部转入和取出
        </label>
      </ActionForm>
      {item.input_id && item.gain_status === "user_confirmed" && (
        <details>
          <summary>撤回这次确认</summary>
          <ActionForm
            label="撤回确认"
            onSave={async (f) => {
              await api.command(
                "/investment-period-inputs/" + item.input_id + "/void",
                "POST",
                {
                  expected_version: item.input_version,
                  reason: String(f.get("reason")),
                },
                "撤回投资确认",
              );
              refresh();
              onClose();
            }}
          >
            <Field label="原因">
              <input name="reason" required />
            </Field>
          </ActionForm>
        </details>
      )}
    </Modal>
  );
}
function BalanceEditor({
  account,
  onClose,
}: {
  account?: Account;
  onClose: () => void;
}) {
  const { accounts, api, refresh, notify } = useApp();
  const [selected, setSelected] = useState(account ? [account.id] : []);
  return (
    <Modal title="给账户更新一下余额" onClose={onClose}>
      <ActionForm
        label="保存本次余额"
        disabled={!selected.length}
        onSave={async (f) => {
          const observations = selected.map((id) => {
            const a = accounts.find((a) => a.id === id)!;
            let value = String(f.get(id)).trim();
            if (a.account_type === "credit") {
              if (!/^\d+(\.\d+)?$/.test(value))
                throw new Error("请填写正数欠款或溢缴款。");
              if (f.get("surplus-" + id) !== "on") value = "-" + value;
            }
            return {
              account_id: id,
              balance: value,
              currency: a.currency,
              as_of: new Date(String(f.get("at-" + id))).toISOString(),
              time_basis: "explicit",
              expected_account_version: a.row_version,
              expected_latest_snapshot_id: a.latest_snapshot?.id || null,
            };
          });
          await api.command(
            "/balance-updates",
            "POST",
            { observations },
            "更新账户余额",
          );
          notify("家里的余额更新好了");
          refresh();
          onClose();
        }}
      >
        <div className="account-checks">
          {accounts
            .filter((a) => a.status === "active")
            .map((a) => (
              <label className="check" key={a.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(a.id)}
                  onChange={(e) =>
                    setSelected((s) =>
                      e.target.checked
                        ? [...s, a.id]
                        : s.filter((id) => id !== a.id),
                    )
                  }
                />
                {a.name} <small>{a.currency}</small>
              </label>
            ))}
        </div>
        {selected.map((id) => {
          const a = accounts.find((a) => a.id === id)!;
          return (
            <section key={id} className="balance-edit-row">
              <h3>{a.name}</h3>
              <div className="form-grid">
                <Field
                  label={
                    a.account_type === "credit"
                      ? "当前总欠款（正数）"
                      : "当前余额（可填零）"
                  }
                >
                  <input
                    name={id}
                    inputMode="decimal"
                    required
                    placeholder="0.00"
                  />
                </Field>
                <Field label="记录时间（设备当地时间）">
                  <input
                    name={"at-" + id}
                    type="datetime-local"
                    required
                    defaultValue={new Date(
                      Date.now() - new Date().getTimezoneOffset() * 60000,
                    )
                      .toISOString()
                      .slice(0, 16)}
                  />
                </Field>
              </div>
              {a.account_type === "credit" && (
                <label className="check">
                  <input type="checkbox" name={"surplus-" + id} />
                  这是多存的溢缴款
                </label>
              )}
              <small className="muted">包含范围：{a.balance_scope}</small>
            </section>
          );
        })}
      </ActionForm>
    </Modal>
  );
}
function SnapshotHistory({
  account,
  onClose,
}: {
  account: Account;
  onClose: () => void;
}) {
  const { api, refresh, notify } = useApp();
  const [cursor, setCursor] = useState<string | null>(null),
    [edit, setEdit] = useState<Snapshot | null>(null);
  const { data, error, loading, reload } = useResource<
    Page<Snapshot> & {
      expected_account_version: number;
      expected_latest_snapshot_id: string | null;
    }
  >(
    `/accounts/${account.id}/snapshots` +
      query({ include_voided: true, cursor }),
  );
  return (
    <Modal title={account.name + " · 余额记录"} onClose={onClose}>
      <ErrorBox error={error} retry={reload} />
      {loading ? (
        <Loading />
      ) : edit ? (
        <ActionForm
          label="保存更正"
          onSave={async (f) => {
            const mode = String(f.get("mode"));
            const body = {
              expected_version: edit.row_version,
              expected_account_version: data!.expected_account_version,
              expected_latest_snapshot_id: data!.expected_latest_snapshot_id,
              reason: String(f.get("reason")),
              reopen_account: false,
              ...(mode === "correct"
                ? {
                    balance: String(f.get("balance")),
                    currency: edit.currency,
                    as_of: String(f.get("at")),
                    time_basis: edit.time_basis,
                  }
                : {}),
            };
            await api.command(
              `/snapshots/${edit.id}/${mode}`,
              "POST",
              body,
              "更正余额记录",
            );
            notify("余额记录已更新");
            refresh();
            setEdit(null);
          }}
        >
          <Field label="操作">
            <select name="mode">
              <option value="correct">更正金额或时间</option>
              <option value="void">作废这次记录</option>
            </select>
          </Field>
          <Field label="余额（欠款填负数）">
            <input name="balance" defaultValue={edit.balance} />
          </Field>
          <Field label="时间">
            <input name="at" defaultValue={edit.as_of} />
          </Field>
          <Field label="更正原因">
            <input name="reason" required />
          </Field>
          <Button onClick={() => setEdit(null)}>返回记录列表</Button>
        </ActionForm>
      ) : (
        <>
          {data?.items.map((s) => (
            <div className="list-row" key={s.id}>
              <div>
                <strong>{money(s.balance, s.currency)}</strong>
                <small>{day(s.as_of)}</small>
              </div>
              {s.status === "active" ? (
                <Button onClick={() => setEdit(s)}>更正</Button>
              ) : (
                <Tag>已作废</Tag>
              )}
            </div>
          ))}
          {data?.next_cursor && (
            <Button onClick={() => setCursor(data.next_cursor)}>
              更早的记录
            </Button>
          )}
          {!data?.items.length && <Empty title="还没有余额记录" />}
        </>
      )}
    </Modal>
  );
}
function ScreenshotUpload({ onClose }: { onClose: () => void }) {
  const { api, notify, refresh } = useApp();
  const keyName = "vl-capture:" + api.subject;
  const [key, setKey] = useState(sessionStorage.getItem(keyName));
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<Error | null>(null);
  function receive(result: Receipt) {
    if (result.status === "processing") {
      notify("截图还在识别，稍后可以查询结果。");
      return;
    }
    sessionStorage.removeItem(keyName);
    setKey(null);
    if (result.status === "needs_confirmation") {
      navigate("/review/" + result.request_id);
      onClose();
    } else if (result.status === "committed") {
      refresh();
      notify("余额已更新");
      onClose();
    } else {
      notify(
        result.status === "rejected"
          ? "这次识别已取消"
          : "这次识别未保存，可以重新上传。",
      );
    }
  }
  async function recover(cancel = false) {
    setBusy(true);
    setError(null);
    try {
      receive(
        await api.request<Receipt>(
          "/ingestion-requests/by-key/" + key + (cancel ? "/cancel" : ""),
          {
            method: cancel ? "POST" : "GET",
          },
        ),
      );
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="用截图更新余额" onClose={onClose}>
      <ErrorBox error={error} />
      {key ? (
        <div>
          <p>
            {busy
              ? "正在识别或查询，请稍候…"
              : "有一张截图还未确认结果，请先查询。"}
          </p>
          <div className="actions">
            <Button
              disabled={busy}
              kind="primary"
              onClick={() => void recover()}
            >
              查询识别结果
            </Button>
            <Button disabled={busy} onClick={() => void recover(true)}>
              取消这次识别
            </Button>
          </div>
        </div>
      ) : (
        <ActionForm
          label="识别截图"
          onSave={async (f) => {
            const file = f.get("file") as File;
            if (
              !file?.size ||
              file.size > 10 * 1024 * 1024 ||
              !["image/png", "image/jpeg"].includes(file.type)
            )
              throw new Error("请选择 10 MiB 以内的 PNG 或 JPEG 图片。");
            const base64 = await new Promise<string>((resolve, reject) => {
              const reader = new FileReader();
              reader.onload = () =>
                resolve(String(reader.result).split(",")[1]);
              reader.onerror = () =>
                reject(new Error("图片读取失败，请重新选择。"));
              reader.readAsDataURL(file);
            });
            const identity = crypto.randomUUID();
            sessionStorage.setItem(keyName, identity);
            setKey(identity);
            setBusy(true);
            setError(null);
            try {
              receive(
                await api.request<Receipt>("/balance-captures", {
                  method: "POST",
                  body: JSON.stringify({
                    idempotency_key: identity,
                    captured_at: new Date().toISOString(),
                    image: { mime_type: file.type, base64 },
                  }),
                }),
              );
            } catch (e) {
              setError(e as Error);
              if (
                e instanceof ApiError &&
                e.status >= 400 &&
                e.status < 500 &&
                ![401, 408, 429].includes(e.status)
              ) {
                sessionStorage.removeItem(keyName);
                setKey(null);
              }
            } finally {
              setBusy(false);
            }
          }}
        >
          <p>选择显示账户总余额的截图，识别后可以核对账户与金额。</p>
          <Field label="账户余额截图">
            <input
              name="file"
              type="file"
              accept="image/png,image/jpeg"
              required
            />
          </Field>
        </ActionForm>
      )}
    </Modal>
  );
}
export function WealthPage() {
  const { accounts, api, timezone, refresh, notify } = useApp();
  const report = useResource<Wealth>("/reports/wealth");
  const now = today(timezone);
  const history = useResource<{ points: (Wealth & { as_of: string })[] }>(
    "/reports/wealth-history" +
      query({ from: Number(now.slice(0, 4)) - 1 + now.slice(4), to: now }),
  );
  const investments = useResource<{
    items: Investment[];
    unavailable: { account_id: string; account_name: string }[];
  }>("/reports/investments");
  const [balance, setBalance] = useState<Account | true | null>(null),
    [snapshot, setSnapshot] = useState<Account | null>(null),
    [investment, setInvestment] = useState<Investment | null>(null),
    [upload, setUpload] = useState(false),
    [error, setError] = useState<Error | null>(null);
  return (
    <>
      <Heading
        eyebrow="GROWING OUR LITTLE WORLD"
        title="家庭财富"
        actions={
          <>
            <Button onClick={() => setUpload(true)}>
              <ImageUp size={16} />
              上传余额截图
            </Button>
            <Button kind="primary" onClick={() => setBalance(true)}>
              <Plus size={16} />
              更新余额
            </Button>
          </>
        }
      >
        家里的钱放在哪里，我们一起心里有数。
      </Heading>
      <ErrorBox error={report.error || error} retry={report.reload} />
      {report.loading ? (
        <Loading />
      ) : (
        report.data && (
          <>
            <WealthStats report={report.data} />
            {!report.data.coverage.complete && (
              <div className="notice">
                部分账户余额或汇率尚未补齐，目前显示已知金额。
              </div>
            )}
            {report.data.coverage.stale_fx_currencies.length > 0 && (
              <div className="notice warning">
                部分币种使用了超过 7 天的旧汇率。
              </div>
            )}
            <div className="section-heading">
              <h2>
                家里的账户{" "}
                <span className="count">
                  {accounts.filter((a) => a.status === "active").length}
                </span>
              </h2>
              <Button
                kind="ghost"
                onClick={async () => {
                  try {
                    await api.command(
                      "/reports/refresh-fx",
                      "POST",
                      {},
                      "更新参考汇率",
                    );
                    refresh();
                    notify("参考汇率已更新");
                  } catch (e) {
                    setError(e as Error);
                  }
                }}
              >
                更新参考汇率
              </Button>
            </div>
            <div className="accounts-grid">
              {report.data.accounts.map((a) => (
                <Card key={a.account_id} className="account-card">
                  <div className="account-card-top">
                    <span className="transaction-icon">
                      <Wallet size={18} />
                    </span>
                    <span>
                      <strong>{a.name}</strong>
                      <small>
                        {accountLabel[a.account_type]} · {a.currency}
                      </small>
                    </span>
                    <Tag
                      tone={a.balance === null || a.needs_update ? "amber" : ""}
                    >
                      {a.balance === null
                        ? "待记录"
                        : a.very_stale
                          ? "超过三个月"
                          : a.needs_update
                            ? "该更新了"
                            : "已记录"}
                    </Tag>
                  </div>
                  <strong className="account-value">
                    {money(a.balance, a.currency)}
                  </strong>
                  <p>
                    {a.account_type === "credit" && Number(a.balance) > 0
                      ? "信用卡溢缴款 · "
                      : a.account_type === "credit"
                        ? "欠款 · "
                        : ""}
                    {a.as_of
                      ? day(a.as_of) + " 更新"
                      : "记录余额后，会计入家庭财富"}
                  </p>
                  <div className="account-card-actions">
                    <Button
                      kind="ghost"
                      onClick={() =>
                        setSnapshot(
                          accounts.find((x) => x.id === a.account_id)!,
                        )
                      }
                    >
                      <History size={14} />
                      历史
                    </Button>
                    <Button
                      onClick={() =>
                        setBalance(accounts.find((x) => x.id === a.account_id)!)
                      }
                    >
                      更新余额
                      <ArrowUpRight size={14} />
                    </Button>
                  </div>
                </Card>
              ))}
            </div>
            {!report.data.accounts.length && (
              <Card>
                <Empty title="先把家里的账户加进来">
                  <a href="#/settings">添加第一个账户</a>
                </Empty>
              </Card>
            )}
            <div className="two-columns">
              <Card title="家底的变化">
                {history.data ? (
                  <WealthHistory points={history.data.points} />
                ) : history.error ? (
                  <ErrorBox error={history.error} retry={history.reload} />
                ) : (
                  <Loading />
                )}
              </Card>
              <Card title="资产的稳健与波动">
                <div className="risk-list">
                  {report.data.risk_buckets.map((r, i) => (
                    <div key={r.risk_level}>
                      <span className={"category-dot color-" + i} />
                      <span>{riskLabel[r.risk_level] || "未分类"}</span>
                      <strong>
                        {money(r.amount, report.data!.reporting_currency)}
                      </strong>
                      <small>
                        {r.percentage === null ? "—" : r.percentage + "%"}
                      </small>
                    </div>
                  ))}
                </div>
                <p className="muted small">
                  占已知正资产的比例。负债不参与计算；缺少汇率的资产暂未包含。
                </p>
              </Card>
            </div>
          </>
        )
      )}
      <Card title="投资这段时间的变化">
        <ErrorBox error={investments.error} retry={investments.reload} />
        {investments.data?.items.length ? (
          investments.data.items.map((i) => (
            <button
              key={i.id}
              className="inbox-row investment-row"
              onClick={() => setInvestment(i)}
            >
              <div>
                <strong>{i.account_name}</strong>
                <small>
                  {day(i.period_start)} — {day(i.period_end)}
                </small>
              </div>
              <Tag tone={i.gain_status === "estimated" ? "amber" : ""}>
                {i.gain_status === "estimated" ? "估算收益" : "已核对投入"}
              </Tag>
              <strong className={Number(i.gain) >= 0 ? "positive" : ""}>
                {money(i.gain, i.currency)}
              </strong>
              <ArrowUpRight size={17} />
            </button>
          ))
        ) : (
          <Empty title="更新两次投资余额，就能看看变化" />
        )}
        <p className="small muted">
          估算收益暂按没有额外投入或取出计算。点开一段时间，补充投入与取出后会重新计算。不同币种分开查看。
        </p>
        {investments.data?.unavailable.map((a) => (
          <p className="small muted" key={a.account_id}>
            {a.account_name}：还需要补充余额记录。
          </p>
        ))}
      </Card>
      {balance && (
        <BalanceEditor
          account={balance === true ? undefined : balance}
          onClose={() => setBalance(null)}
        />
      )}{" "}
      {snapshot && (
        <SnapshotHistory account={snapshot} onClose={() => setSnapshot(null)} />
      )}{" "}
      {investment && (
        <InvestmentEditor
          item={investment}
          onClose={() => setInvestment(null)}
        />
      )}{" "}
      {upload && <ScreenshotUpload onClose={() => setUpload(false)} />}
    </>
  );
}

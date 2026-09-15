import { useState } from "react";
import {
  Plus,
  Smartphone,
  Wallet,
  Tag as TagIcon,
  SlidersHorizontal,
  LogOut,
} from "lucide-react";
import { useApp, useResource } from "../lib/context";
import {
  today,
  day,
  money,
  accountLabel,
  currencies,
  riskLabel,
} from "../lib/format";
import {
  Heading,
  Card,
  Button,
  Field,
  ActionForm,
  Modal,
  ErrorBox,
  Empty,
  Tag,
} from "../components/ui";
import type { Account, Category, Page, Review, Snapshot } from "../types";
type Device = {
  device_id: string;
  device_name: string;
  status: string;
  last_seen_at: string | null;
};
function AccountEditor({
  item,
  onClose,
}: {
  item?: Account;
  onClose: () => void;
}) {
  const { api, timezone, notify, refresh } = useApp();
  const [type, setType] = useState(item?.account_type || "savings");
  const aliases = useResource<
    Page<{ id: string; alias: string; status: string; row_version: number }>
  >(item ? `/accounts/${item.id}/aliases` : null);
  return (
    <Modal title={item ? "编辑账户" : "添加账户"} onClose={onClose}>
      <ActionForm
        onSave={async (f) => {
          const body = {
            name: String(f.get("name")),
            balance_scope: String(f.get("scope")),
            risk_level:
              type === "credit" ? null : String(f.get("risk")) || null,
            statement_import_enabled: f.get("statement") === "on",
          };
          await api.command(
            item ? "/accounts/" + item.id : "/accounts",
            item ? "PATCH" : "POST",
            item
              ? { ...body, expected_version: item.row_version }
              : {
                  ...body,
                  account_type: type,
                  currency: String(f.get("currency")),
                  opened_on: String(f.get("opened")),
                },
            item ? "修改账户" : "添加账户",
          );
          refresh();
          notify("账户设置已保存");
          onClose();
        }}
      >
        <Field label="账户名称">
          <input
            name="name"
            required
            maxLength={120}
            defaultValue={item?.name || ""}
            placeholder="比如：日常钱包、家庭储蓄"
          />
        </Field>
        <div className="form-grid">
          <Field label="账户类型">
            <select
              value={type}
              disabled={!!item}
              onChange={(e) => setType(e.target.value)}
            >
              {Object.entries(accountLabel).map(([k, v]) => (
                <option value={k} key={k}>
                  {v}
                </option>
              ))}
            </select>
          </Field>
          <Field label="币种">
            <select
              name="currency"
              disabled={!!item}
              defaultValue={item?.currency || "CNY"}
            >
              {currencies.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          </Field>
        </div>
        <Field
          label="这个余额包含什么？"
          hint="比如：仅活期存款，不含同一家银行的理财。避免不同账户重复统计。"
        >
          <input
            name="scope"
            required
            maxLength={120}
            defaultValue={item?.balance_scope || ""}
          />
        </Field>
        {type !== "credit" && (
          <Field label="资产的波动程度">
            <select name="risk" defaultValue={item?.risk_level || ""}>
              <option value="">暂不分类</option>
              {Object.entries(riskLabel)
                .filter(([k]) => k !== "unclassified")
                .map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
            </select>
          </Field>
        )}
        {!item && (
          <Field label="从哪天开始记录">
            <input
              name="opened"
              type="date"
              required
              defaultValue={today(timezone)}
              max={today(timezone)}
            />
          </Field>
        )}
        <label className="check">
          <input
            name="statement"
            type="checkbox"
            defaultChecked={item?.statement_import_enabled ?? true}
          />
          允许导入这个账户的 PDF 账单
        </label>
      </ActionForm>
      {item && (
        <details>
          <summary>账户别名</summary>
          <p className="small muted">
            截图或账单里出现的其他名称，可帮助找到这个账户。
          </p>
          <ErrorBox error={aliases.error} />
          {aliases.data?.items.map((a) => (
            <details key={a.id}>
              <summary>
                {a.alias} {a.status !== "active" ? "（已停用）" : ""}
              </summary>
              <ActionForm
                label="保存别名"
                onSave={async (f) => {
                  await api.command(
                    `/accounts/${item.id}/aliases/${a.id}`,
                    "PATCH",
                    {
                      alias: String(f.get("alias")),
                      status: String(f.get("status")),
                      expected_version: a.row_version,
                    },
                    "修改账户别名",
                  );
                  aliases.reload();
                }}
              >
                <Field label="别名">
                  <input
                    name="alias"
                    required
                    maxLength={120}
                    defaultValue={a.alias}
                  />
                </Field>
                <Field label="使用状态">
                  <select name="status" defaultValue={a.status}>
                    <option value="active">启用</option>
                    <option value="inactive">停用</option>
                  </select>
                </Field>
              </ActionForm>
            </details>
          ))}
          <ActionForm
            label="添加别名"
            onSave={async (f) => {
              await api.command(
                `/accounts/${item.id}/aliases`,
                "POST",
                { alias: String(f.get("alias")) },
                "添加账户别名",
              );
              aliases.reload();
            }}
          >
            <Field label="新的账户别名">
              <input name="alias" required maxLength={120} />
            </Field>
          </ActionForm>
        </details>
      )}
      {item && <AccountLifecycle item={item} onClose={onClose} />}
    </Modal>
  );
}
function AccountLifecycle({
  item,
  onClose,
}: {
  item: Account;
  onClose: () => void;
}) {
  const { api, timezone, refresh, notify } = useApp();
  const [action, setAction] = useState(
    item.status === "closed" ? "reopen" : "close",
  );
  const [cursor, setCursor] = useState<string | null>(null);
  const snapshots = useResource<Page<Snapshot>>(
    item.status === "active" && action === "close"
      ? `/accounts/${item.id}/snapshots` +
          (cursor
            ? `?cursor=${encodeURIComponent(cursor)}&limit=100`
            : "?limit=100")
      : null,
  );
  const zero =
    snapshots.data?.items.filter(
      (s) => /^-?0+(\.0+)?$/.test(s.balance) && s.status === "active",
    ) || [];
  if (item.status === "cancelled")
    return <p className="muted">这个账户已取消。</p>;
  return (
    <details>
      <summary>
        {item.status === "closed" ? "重新使用账户" : "不再使用这个账户"}
      </summary>
      <ActionForm
        label={
          action === "reopen"
            ? "重新启用"
            : action === "cancel"
              ? "取消空账户"
              : "确认关闭账户"
        }
        onSave={async (f) => {
          await api.command(
            `/accounts/${item.id}/${action}`,
            "POST",
            {
              expected_version: item.row_version,
              reason: String(f.get("reason")) || null,
              ...(action === "close"
                ? {
                    closed_on: String(f.get("closed")),
                    closing_snapshot_id: String(f.get("snapshot")),
                  }
                : {}),
            },
            "调整账户状态",
          );
          refresh();
          notify("账户状态已更新");
          onClose();
        }}
      >
        {item.status === "active" && (
          <Field label="处理方式">
            <select value={action} onChange={(e) => setAction(e.target.value)}>
              <option value="close">已结清，关闭账户并保留历史</option>
              <option value="cancel">误建的空账户，取消记录</option>
            </select>
          </Field>
        )}
        <p className="notice">
          {action === "close"
            ? "先在家庭财富中记录结清后的零余额，再选择这条余额关闭账户。"
            : action === "cancel"
              ? "只允许取消没有财务记录的空账户。"
              : "恢复后可以继续记录这个账户的余额和支出。"}
        </p>
        {action === "close" && (
          <>
            <ErrorBox error={snapshots.error} retry={snapshots.reload} />
            <Field label="用于结清的零余额">
              <select name="snapshot" required>
                <option value="">请选择零余额记录</option>
                {zero.map((s) => (
                  <option key={s.id} value={s.id}>
                    {day(s.as_of)} · {money(s.balance, s.currency)}
                  </option>
                ))}
              </select>
            </Field>
            {snapshots.data?.next_cursor && (
              <Button onClick={() => setCursor(snapshots.data!.next_cursor)}>
                查找更早的零余额
              </Button>
            )}
            {cursor && (
              <Button onClick={() => setCursor(null)}>回到最新余额</Button>
            )}
            <Field label="关闭日期">
              <input
                name="closed"
                type="date"
                required
                defaultValue={today(timezone)}
                max={today(timezone)}
              />
            </Field>
          </>
        )}
        <Field label="说明">
          <input name="reason" required />
        </Field>
      </ActionForm>
    </details>
  );
}
function CategoryEditor({
  item,
  onClose,
}: {
  item?: Category;
  onClose: () => void;
}) {
  const { api, notify, refresh } = useApp();
  return (
    <Modal title={item ? "修改分类" : "添加一个分类"} onClose={onClose}>
      <ActionForm
        onSave={async (f) => {
          const body = {
            name: String(f.get("name")),
            description: String(f.get("description")) || null,
          };
          await api.command(
            item ? "/categories/" + item.id : "/categories",
            item ? "PATCH" : "POST",
            item
              ? {
                  ...body,
                  status: String(f.get("status")),
                  expected_version: item.row_version,
                }
              : { ...body, type: String(f.get("type")) },
            "保存分类",
          );
          refresh();
          notify("分类已保存");
          onClose();
        }}
      >
        <Field label="分类名称">
          <input
            name="name"
            required
            defaultValue={item?.name || ""}
            maxLength={100}
          />
        </Field>
        {!item && (
          <Field label="分类用途">
            <select name="type">
              <option value="expense">支出</option>
              <option value="income">收入</option>
            </select>
          </Field>
        )}
        <Field label="说明（可选）">
          <input
            name="description"
            defaultValue={item?.description || ""}
            maxLength={500}
          />
        </Field>
        {item && (
          <Field label="是否使用">
            <select name="status" defaultValue={item.status}>
              <option value="active">继续使用</option>
              <option value="inactive">暂时停用</option>
            </select>
          </Field>
        )}
      </ActionForm>
    </Modal>
  );
}
export function SettingsPage({ onLogout }: { onLogout: () => Promise<void> }) {
  const { api, accounts, categories, refresh, notify } = useApp();
  const [tab, setTab] = useState("accounts"),
    [account, setAccount] = useState<Account | true | null>(null),
    [category, setCategory] = useState<Category | true | null>(null),
    [device, setDevice] = useState(false),
    [credential, setCredential] = useState(""),
    [revoke, setRevoke] = useState<Device | null>(null);
  const devices = useResource<Page<Device>>(
    tab === "devices" ? "/devices" : null,
  );
  const settings = useResource<Review>(
    tab === "preferences" ? "/review" : null,
  );
  return (
    <>
      <Heading title="设置">管理账户、分类、设备和提醒。</Heading>
      <div className="settings-layout">
        <nav className="settings-nav" aria-label="设置分类">
          {[
            ["accounts", "账户", Wallet],
            ["categories", "分类", TagIcon],
            ["devices", "快捷指令设备", Smartphone],
            ["preferences", "提醒与登录", SlidersHorizontal],
          ].map(([id, label, Icon]) => {
            const I = Icon as typeof Wallet;
            return (
              <button
                className={tab === id ? "selected" : ""}
                onClick={() => setTab(String(id))}
                key={String(id)}
              >
                <I size={17} />
                {String(label)}
              </button>
            );
          })}
        </nav>
        <div>
          {tab === "accounts" && (
            <Card
              title="家里的账户"
              action={
                <Button kind="primary" onClick={() => setAccount(true)}>
                  <Plus size={15} />
                  添加账户
                </Button>
              }
            >
              {accounts.map((a) => (
                <div key={a.id} className="list-row">
                  <div>
                    <strong>{a.name}</strong>
                    <small>
                      {accountLabel[a.account_type]} · {a.currency} ·{" "}
                      {a.balance_scope}
                    </small>
                  </div>
                  <Tag>
                    {a.status === "active"
                      ? "使用中"
                      : a.status === "closed"
                        ? "已关闭"
                        : "已取消"}
                  </Tag>
                  <Button onClick={() => setAccount(a)}>设置</Button>
                </div>
              ))}
              {!accounts.length && <Empty title="从常用的账户开始吧" />}
            </Card>
          )}
          {tab === "categories" && (
            <Card
              title="日常的消费与收入分类"
              action={
                <Button kind="primary" onClick={() => setCategory(true)}>
                  <Plus size={15} />
                  添加分类
                </Button>
              }
            >
              {categories.map((c) => (
                <div className="list-row" key={c.id}>
                  <div>
                    <strong>{c.name}</strong>
                    <small>
                      {c.category_type === "income" ? "收入" : "支出"}
                      {c.is_fallback ? " · 默认分类" : ""}
                      {c.status === "inactive" ? " · 已停用" : ""}
                    </small>
                  </div>
                  <Button onClick={() => setCategory(c)}>修改</Button>
                </div>
              ))}
            </Card>
          )}
          {tab === "devices" && (
            <Card
              title="连接快捷指令的设备"
              action={
                <Button kind="primary" onClick={() => setDevice(true)}>
                  <Plus size={15} />
                  添加设备
                </Button>
              }
            >
              <p className="muted">
                这里是你自己的快捷指令设备。撤销后，该设备就不能再访问账本。
              </p>
              <ErrorBox error={devices.error} retry={devices.reload} />
              {devices.data?.items.map((d) => (
                <div className="list-row" key={d.device_id}>
                  <Smartphone size={20} />
                  <div>
                    <strong>{d.device_name}</strong>
                    <small>最近使用：{day(d.last_seen_at)}</small>
                  </div>
                  <Tag>{d.status === "active" ? "已连接" : "已撤销"}</Tag>
                  {d.status === "active" && (
                    <Button onClick={() => setRevoke(d)}>撤销访问</Button>
                  )}
                </div>
              ))}
            </Card>
          )}
          {tab === "preferences" && (
            <Card title="投资变化提醒">
              <ErrorBox error={settings.error} retry={settings.reload} />
              {settings.data && (
                <ActionForm
                  onSave={async (f) => {
                    await api.command(
                      "/household-settings",
                      "PATCH",
                      {
                        expected_version: settings.data!.settings_row_version,
                        investment_review_change_ratio: String(
                          Number(f.get("threshold")) / 100,
                        ),
                      },
                      "修改投资提醒",
                    );
                    refresh();
                    notify("提醒设置已保存");
                  }}
                >
                  <Field
                    label="余额变化达到多少时，提醒核对投入与取出？"
                    hint="这项提醒帮助区分投资收益与额外投入。"
                  >
                    <div className="inline-field">
                      <input
                        name="threshold"
                        type="number"
                        min="0.01"
                        max="100"
                        step="0.01"
                        required
                        defaultValue={
                          Number(settings.data.investment_review_change_ratio) *
                          100
                        }
                      />
                      <span>%</span>
                    </div>
                  </Field>
                </ActionForm>
              )}
              <hr />
              <h3>这台设备上的登录</h3>
              <p className="muted">
                刷新页面会保持登录。在共用电脑上使用后，记得退出。
              </p>
              <Button
                onClick={() => {
                  void onLogout();
                }}
              >
                <LogOut size={15} />
                退出登录
              </Button>
            </Card>
          )}
        </div>
      </div>
      {account && (
        <AccountEditor
          item={account === true ? undefined : account}
          onClose={() => setAccount(null)}
        />
      )}{" "}
      {category && (
        <CategoryEditor
          item={category === true ? undefined : category}
          onClose={() => setCategory(null)}
        />
      )}{" "}
      {device && (
        <Modal
          title="添加快捷指令设备"
          onClose={() => {
            setDevice(false);
            setCredential("");
          }}
        >
          {credential ? (
            <>
              <p>
                设备已创建。把下面的访问凭据填入你自己的快捷指令中；关闭后不会再次显示。
              </p>
              <textarea readOnly aria-label="设备访问凭据" value={credential} />
              <Button
                kind="primary"
                onClick={() => {
                  setDevice(false);
                  setCredential("");
                }}
              >
                我已保存
              </Button>
            </>
          ) : (
            <ActionForm
              label="创建设备"
              onSave={async (f) => {
                const result = await api.request<{ token: string }>(
                  "/devices",
                  {
                    method: "POST",
                    body: JSON.stringify({
                      device_name: String(f.get("name")),
                      platform: "ios_shortcuts",
                    }),
                  },
                );
                setCredential(result.token);
                devices.reload();
              }}
            >
              <Field label="设备名称">
                <input
                  name="name"
                  required
                  placeholder="比如：我的 iPhone"
                  maxLength={100}
                />
              </Field>
            </ActionForm>
          )}
        </Modal>
      )}{" "}
      {revoke && (
        <Modal
          title={"撤销 " + revoke.device_name + " 的访问？"}
          onClose={() => setRevoke(null)}
        >
          <p>这个设备的快捷指令将无法继续读写账本。</p>
          <ActionForm
            label="确认撤销"
            onSave={async () => {
              await api.command(
                "/devices/" + revoke.device_id + "/revoke",
                "POST",
                {},
                "撤销设备访问",
              );
              devices.reload();
              setRevoke(null);
              notify("已撤销这个设备的访问");
            }}
          />
        </Modal>
      )}
    </>
  );
}

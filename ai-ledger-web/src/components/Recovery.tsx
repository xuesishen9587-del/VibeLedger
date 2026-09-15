import { useEffect, useState } from "react";
import { useApp } from "../lib/context";
import { ErrorBox } from "./ui";
export function Recovery() {
  const { api, refresh, notify } = useApp();
  const [items, setItems] = useState(api.pending()),
    [error, setError] = useState<Error | null>(null),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    const listener = () => setItems(api.pending());
    window.addEventListener("vl-pending", listener);
    return () => window.removeEventListener("vl-pending", listener);
  }, [api]);
  if (!items.length) return null;
  return (
    <aside className="notice warning">
      <strong>有 {items.length} 项保存结果还未确认</strong>
      <p>网络恢复后，重试会查询或完成原操作。</p>
      {items.map((c) => (
        <div className="recovery-row" key={c.key}>
          <span>{c.label}</span>
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                await api.retry(c);
                notify("保存结果已确认");
                refresh();
              } catch (e) {
                setError(e as Error);
              } finally {
                setBusy(false);
              }
            }}
          >
            确认原操作的结果
          </button>
        </div>
      ))}
      <ErrorBox error={error} />
    </aside>
  );
}

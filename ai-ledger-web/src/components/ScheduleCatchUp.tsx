import { useEffect, useState, type ReactNode } from "react";
import { useApp } from "../lib/context";
import { Button } from "./ui";

// Mount the report only after attempting the durable command. GET remains read-only.
export function ScheduleCatchUp({ children }: { children: ReactNode }) {
  const { api, timezone } = useApp();
  const [attempt, setAttempt] = useState(0);
  const [day, setDay] = useState(() => new Date().toLocaleDateString("en-CA", { timeZone: timezone }));
  const [state, setState] = useState<{ busy: boolean; through?: string; failed?: boolean }>({ busy: true });
  useEffect(() => {
    const check = () => setDay(new Date().toLocaleDateString("en-CA", { timeZone: timezone }));
    const timer = window.setInterval(check, 60000);
    window.addEventListener("focus", check);
    return () => { clearInterval(timer); window.removeEventListener("focus", check); };
  }, [timezone]);
  useEffect(() => {
    let active = true;
    setState({ busy: true });
    const run = async () => {
      const path = "/spending-schedules/materialize";
      const pending = api.pending().find(c => c.path === path && c.method === "POST");
      // Recover an uncertain command with its original key before catching up today.
      if (pending) await api.retry(pending);
      const result = await api.command<{ schedules_current_through: string }>(path, "POST", {}, "补记到期月度支出");
      if (!/^\d{4}-\d{2}-\d{2}$/.test(result.schedules_current_through || "")) throw new Error("Missing schedule freshness");
      if (active) setState({ busy: false, through: result.schedules_current_through });
    };
    void run().catch(() => { if (active) setState({ busy: false, failed: true }); });
    return () => { active = false; };
  }, [api, day, attempt]);
  return <>
    <div className="notice" role={state.failed ? "alert" : "status"}>
      {state.busy ? "正在补记到期月度支出，请稍候…" : state.failed ? <>
        月度计划补记未完成，以下支出可能不完整。
        <Button onClick={() => setAttempt(v => v + 1)}>重试补记</Button>
      </> : `月度计划已检查至 ${state.through}`}
    </div>
    {!state.busy && children}
  </>;
}

import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  createClient,
  type SupabaseClient,
  type Session,
} from "@supabase/supabase-js";
import {
  House,
  Wallet,
  ChartNoAxesCombined,
  Inbox,
  Settings,
  Heart,
  LogOut,
  Plus,
} from "lucide-react";
import { Api } from "./lib/api";
import { ApiError } from "./lib/errors";
import { AppContext, navigate, useResource } from "./lib/context";
import { Button, Field, ErrorBox, Loading, ActionForm } from "./components/ui";
import { Recovery } from "./components/Recovery";
const Home = lazy(() =>
  import("./pages/Home").then((m) => ({ default: m.Home })),
);
const SpendingPage = lazy(() =>
  import("./pages/Spending").then((m) => ({ default: m.SpendingPage })),
);
const WealthPage = lazy(() =>
  import("./pages/Wealth").then((m) => ({ default: m.WealthPage })),
);
const StatementPage = lazy(() =>
  import("./pages/Statement").then((m) => ({ default: m.StatementPage })),
);
const ReviewPage = lazy(() =>
  import("./pages/Review").then((m) => ({ default: m.ReviewPage })),
);
const SettingsPage = lazy(() =>
  import("./pages/Settings").then((m) => ({ default: m.SettingsPage })),
);
import type { Account, Category, Page, Review } from "./types";
import "./styles.css";

type Config = { supabaseUrl: string; supabaseKey: string; timezone: string };
const AUTH_STORAGE_KEY = "vibeledger-auth";
function clearFinancialStorage() {
  for (let i = sessionStorage.length - 1; i >= 0; i--) {
    const key = sessionStorage.key(i)!;
    if (key.startsWith("vl-")) sessionStorage.removeItem(key);
  }
}
function Login({
  auth,
  error,
}: {
  auth: SupabaseClient | null;
  error?: Error | null;
}) {
  return (
    <main className="login-page">
      <div className="login-story">
        <span className="brand">
          <span className="brand-mark">
            <House size={24} />
          </span>
          VibeLedger
        </span>
        <div>
          <h1>家庭账本</h1>
          <p>记录支出，查看账户余额和资产。</p>
          <div className="home-art" aria-hidden="true">
            <House size={160} strokeWidth={0.8} />
            <Heart size={28} />
          </div>
        </div>
      </div>
      <section className="login-card">
        <h2>登录</h2>
        <p>使用家庭成员账号登录。</p>
        <ErrorBox error={error} />
        {!auth ? (
          <p className="notice">登录服务尚未配置，请配置家庭账本的登录服务。</p>
        ) : (
          <ActionForm
            label="登录"
            onSave={async (form) => {
              const { error } = await auth.auth.signInWithPassword({
                email: String(form.get("email")).trim(),
                password: String(form.get("password")),
              });
              if (error)
                throw new Error("登录未成功，请检查邮箱和密码，稍后再试。");
            }}
          >
            <Field label="邮箱">
              <input
                autoComplete="username"
                type="email"
                name="email"
                required
                placeholder="you@example.com"
              />
            </Field>
            <Field label="密码">
              <input
                autoComplete="current-password"
                type="password"
                name="password"
                required
              />
            </Field>
            <p className="muted small">
              登录会保留在这台设备上。共用电脑使用后，请记得退出。
            </p>
          </ActionForm>
        )}
      </section>
    </main>
  );
}
function Shell({
  session,
  auth,
  config,
}: {
  session: Session;
  auth: SupabaseClient;
  config: Config;
}) {
  const [accounts, setAccounts] = useState<Account[]>([]),
    [categories, setCategories] = useState<Category[]>([]),
    [revision, setRevision] = useState(0),
    [ready, setReady] = useState(false),
    [error, setError] = useState<Error | null>(null),
    [toast, setToast] = useState("");
  const [route, setRoute] = useState(location.hash.slice(1) || "/home");
  const api = useMemo(
    () =>
      new Api(session.user.id, async () => {
        const { data, error } = await auth.auth.getSession();
        if (error) throw new Error("暂时无法恢复登录，请稍后重试。");
        return data.session?.user.id === session.user.id
          ? data.session.access_token
          : null;
      }),
    [auth, session.user.id],
  );
  useEffect(() => () => api.dispose(), [api]);
  useEffect(() => {
    const fn = () => setRoute(location.hash.slice(1) || "/home");
    window.addEventListener("hashchange", fn);
    return () => window.removeEventListener("hashchange", fn);
  }, []);
  useEffect(() => {
    let active = true;
    setError(null);
    Promise.all([
      api.request<Page<Account>>("/accounts"),
      api.request<Page<Category>>("/categories"),
    ]).then(
      ([a, c]) => {
        if (active) {
          setAccounts(a.items);
          setCategories(c.items);
          setReady(true);
        }
      },
      (e) => {
        if (active) setError(e);
      },
    );
    return () => {
      active = false;
    };
  }, [api, revision]);
  useEffect(() => {
    if (toast) {
      const t = setTimeout(() => setToast(""), 5000);
      return () => clearTimeout(t);
    }
  }, [toast]);
  const refresh = () => setRevision((v) => v + 1);
  const logout = async () => {
    clearFinancialStorage();
    localStorage.setItem("vibeledger-logout", crypto.randomUUID());
    api.dispose();
    const { error } = await auth.auth.signOut({ scope: "local" });
    if (error) {
      localStorage.removeItem(AUTH_STORAGE_KEY);
      location.reload();
    }
  };
  if (!ready)
    return error ? (
      <div className="gate">
        <ErrorBox error={error} retry={refresh} />
        <Button
          onClick={() => {
            void logout();
          }}
        >
          换一个账号登录
        </Button>
      </div>
    ) : (
      <Loading />
    );
  return (
    <AppContext.Provider
      value={{
        api,
        accounts,
        categories,
        timezone: config.timezone,
        revision,
        refresh,
        notify: setToast,
      }}
    >
      <div className="app-shell">
        <aside className="sidebar">
          <a href="#/home" className="brand">
            <span className="brand-mark">
              <House size={23} />
            </span>
            <span>
              VibeLedger<small>家庭账本</small>
            </span>
          </a>
          <Navigation route={route} />
          <div className="sidebar-bottom">
            <button
              className="logout"
              onClick={() => {
                void logout();
              }}
            >
              <LogOut size={16} />
              退出登录
            </button>
          </div>
        </aside>
        <div className="main-area">
          <div className="topbar">
            <span>家庭账本</span>
            <div className="login-identity" aria-label="当前登录身份">
              <span>当前登录</span>
              <strong>
                {session.user.email || session.user.phone || session.user.id}
              </strong>
            </div>
          </div>
          <main className="page" id="main-content">
            <ErrorBox error={error} retry={refresh} />
            <Recovery />
            <Suspense fallback={<Loading />}>
              {route.startsWith("/statement") ? (
                <StatementPage identity={route.split("/")[2]} />
              ) : route.startsWith("/spending") ? (
                <SpendingPage />
              ) : route.startsWith("/wealth") ? (
                <WealthPage />
              ) : route.startsWith("/review") ? (
                <ReviewPage identity={route.split("/")[2]} />
              ) : route.startsWith("/settings") ? (
                <SettingsPage onLogout={logout} />
              ) : (
                <Home />
              )}
            </Suspense>
          </main>
          <footer className="page-footer">
            <span>VibeLedger</span>
          </footer>
        </div>
        {toast && (
          <div className="toast" role="status">
            {toast}
          </div>
        )}
        <button
          className="mobile-add"
          aria-label="记一笔"
          onClick={() => navigate("/spending/new")}
        >
          <Plus />
        </button>
      </div>
    </AppContext.Provider>
  );
}
function Navigation({ route }: { route: string }) {
  const { data } = useResource<Review>("/review");
  const counts = data?.counts;
  const total = counts
    ? counts.transactions +
      counts.drafts +
      counts.schedule_occurrences +
      counts.unusual_investment_estimates
    : 0;
  return (
    <nav aria-label="主要导航">
      {[
        ["/home", "总览", House],
        ["/spending", "日常支出", Wallet],
        ["/wealth", "家庭财富", ChartNoAxesCombined],
        ["/review", "待处理", Inbox],
        ["/settings", "设置", Settings],
      ].map(([path, label, Icon]) => {
        const P = path as string;
        const I = Icon as typeof House;
        return (
          <a
            key={P}
            href={"#" + P}
            aria-current={
              route.startsWith(P) ||
              (P === "/spending" && route.startsWith("/statement"))
                ? "page"
                : undefined
            }
          >
            <I size={20} />
            <span>{String(label)}</span>
            {P === "/review" && total > 0 && <b>{total}</b>}
          </a>
        );
      })}
    </nav>
  );
}
function App() {
  const [config, setConfig] = useState<Config | null>(null),
    [auth, setAuth] = useState<SupabaseClient | null>(null),
    [session, setSession] = useState<Session | null>(null),
    [loading, setLoading] = useState(true),
    [error, setError] = useState<Error | null>(null),
    [blocked, setBlocked] = useState(false);
  useEffect(() => {
    let active = true;
    let unsubscribe: (() => void) | undefined;
    fetch("/config", { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error("暂时无法读取登录配置。");
        return r.json();
      })
      .then(async (c: Config) => {
        if (!active) return;
        setConfig(c);
        if (!c.supabaseUrl || !c.supabaseKey) {
          setLoading(false);
          return;
        }
        try {
          localStorage.setItem("vl-storage-check", "1");
          localStorage.removeItem("vl-storage-check");
          sessionStorage.setItem("vl-storage-check", "1");
          sessionStorage.removeItem("vl-storage-check");
        } catch {
          throw new ApiError("STORAGE_UNAVAILABLE");
        }
        const client = createClient(c.supabaseUrl, c.supabaseKey, {
          auth: {
            storageKey: AUTH_STORAGE_KEY,
            persistSession: true,
            autoRefreshToken: true,
            detectSessionInUrl: false,
          },
        });
        setAuth(client);
        const { data } = client.auth.onAuthStateChange((event, s) => {
          if (!active) return;
          // Preserve same-user recovery after expiry; explicit logout clears it.
          if (s) {
            const previous = sessionStorage.getItem("vl-session-owner");
            if (previous && previous !== s.user.id) clearFinancialStorage();
            sessionStorage.setItem("vl-session-owner", s.user.id);
          }
          if (event === "SIGNED_IN") setBlocked(false);
          setSession(s);
          setLoading(false);
        });
        unsubscribe = () => data.subscription.unsubscribe();
        const restored = await client.auth.getSession();
        if (restored.error)
          throw new Error("暂时无法恢复登录，请稍后刷新重试。");
        if (active) {
          setSession(restored.data.session);
          setLoading(false);
        }
      })
      .catch((e) => {
        if (active) {
          setError(e);
          setLoading(false);
        }
      });
    return () => {
      active = false;
      unsubscribe?.();
    };
  }, []);
  useEffect(() => {
    const fn = () => {
      setBlocked(true);
      setError(new ApiError("UNAUTHORIZED"));
    };
    const logoutElsewhere = (e: StorageEvent) => {
      if (e.key !== "vibeledger-logout") return;
      clearFinancialStorage();
      setSession(null);
      setBlocked(true);
    };
    window.addEventListener("vl-unauthorized", fn);
    window.addEventListener("storage", logoutElsewhere);
    return () => {
      window.removeEventListener("vl-unauthorized", fn);
      window.removeEventListener("storage", logoutElsewhere);
    };
  }, []);
  if (loading) return <Loading />;
  if (!session || !auth || !config || blocked)
    return <Login auth={auth} error={error} />;
  return (
    <Shell
      key={session.user.id}
      session={session}
      auth={auth}
      config={config}
    />
  );
}
createRoot(document.getElementById("root")!).render(<App />);

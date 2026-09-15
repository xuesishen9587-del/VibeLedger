import { createContext, useContext, useEffect, useState } from "react";
import type { Api } from "./api";
import type { Account, Category } from "../types";
export const AppContext = createContext<{
  api: Api;
  accounts: Account[];
  categories: Category[];
  timezone: string;
  revision: number;
  refresh: () => void;
  notify: (message: string) => void;
}>(null!);
export const useApp = () => useContext(AppContext);
export function useResource<T>(path: string | null) {
  const { api, revision } = useApp();
  const [state, setState] = useState<{
    data?: T;
    error?: Error;
    loading: boolean;
  }>({ loading: true });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let active = true;
    setState({ loading: !!path });
    if (path)
      api.request<T>(path).then(
        (data) => {
          if (active) setState({ data, loading: false });
        },
        (error) => {
          if (active) setState({ error, loading: false });
        },
      );
    return () => {
      active = false;
    };
  }, [api, path, revision, tick]);
  return { ...state, reload: () => setTick((t) => t + 1) };
}
export function navigate(path: string) {
  window.location.hash = path;
}

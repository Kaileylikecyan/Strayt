import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { SyncSnapshot, WriteOp, Writables } from "@strayt/sync-engine";
import { SyncFlushError } from "@strayt/sync-engine";
import { createApiClient } from "@strayt/api-client";
import { createAppApi, isNetworkError, type AppApi, type AppDomain } from "./api";
import { clearConfig, loadConfig, saveConfig, type ServerConfig } from "./config";

export type SyncState = "synced" | "syncing" | "offline";

interface SaveInput {
  entity: Writables;
  entity_id: string;
  patch: Record<string, unknown>;
}

interface AppContextValue {
  config: ServerConfig | null;
  api: AppApi | null;
  domain: AppDomain | null;
  snapshot: SyncSnapshot | null;
  syncState: SyncState;
  pending: number;
  lastError: string | null;
  /** 探活 + 查是否已设口令。只做只读探测，不建长连接。 */
  probeServer: (baseUrl: string) => Promise<{ ok: boolean; passwordSet: boolean }>;
  /** 用口令换会话令牌（登录/创建口令的公共尾巴）。 */
  authenticate: (baseUrl: string, password: string, mode: "login" | "setup") => Promise<void>;
  connect: (config: ServerConfig) => Promise<void>;
  disconnect: () => void;
  /** 改口令。换完服务端会作废旧令牌，所以这里顺带换成新令牌。 */
  changePassword: (oldPassword: string, newPassword: string) => Promise<void>;
  refresh: () => Promise<void>;
  flushNow: () => Promise<void>;
  save: (input: SaveInput) => Promise<"direct" | "queued" | "error">;
  clearError: () => void;
}

const AppContext = createContext<AppContextValue | null>(null);

function applyPatchToSnapshot(prev: SyncSnapshot, op: WriteOp): SyncSnapshot {
  const next: SyncSnapshot = {
    ...prev,
    projects: [...prev.projects],
    files: [...prev.files],
    pieces: [...prev.pieces],
    pairs: [...prev.pairs],
    goals: [...prev.goals],
    plans: [...prev.plans],
    checkins: [...prev.checkins],
  };
  if (op.entity === "piece_progress") {
    const idx = next.pieces.findIndex((p) => p.id === op.entity_id);
    if (idx >= 0) {
      const piece = { ...next.pieces[idx] };
      if (typeof op.patch.last_pos === "number") piece.last_pos = op.patch.last_pos;
      if (typeof op.patch.recited === "boolean") piece.recited = op.patch.recited;
      next.pieces[idx] = piece;
    }
  } else if (op.entity === "checkin") {
    const items = Array.isArray(op.patch.items_json) ? (op.patch.items_json as string[]) : [];
    const existing = next.checkins.findIndex((c) => c.date === op.entity_id);
    const entry = {
      date: op.entity_id,
      items_json: items,
      updated_at: new Date().toISOString().slice(0, 19),
    };
    if (existing >= 0) next.checkins[existing] = entry;
    else next.checkins.push(entry);
  } else if (op.entity === "plan") {
    const idx = next.plans.findIndex((p) => p.id === op.entity_id);
    if (idx >= 0) {
      const plan = { ...next.plans[idx], ...op.patch } as SyncSnapshot["plans"][number];
      next.plans[idx] = plan;
    }
  }
  return next;
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [config, setConfigState] = useState<ServerConfig | null>(() => loadConfig());
  const [apiState, setApiState] = useState<AppApi | null>(null);
  const [snapshot, setSnapshot] = useState<SyncSnapshot | null>(null);
  const [syncState, setSyncState] = useState<SyncState>("synced");
  const [lastError, setLastError] = useState<string | null>(null);
  const apiRef = useRef<AppApi | null>(null);

  const currentApi = apiRef.current;

  // 刷新页面后自动重连：重建 engine 但不清写队列，直接全量拉取覆盖 snapshot
  useEffect(() => {
    if (!config || apiRef.current) return;
    const built = createAppApi(config);
    apiRef.current = built;
    setApiState(built);
    void built.engine
      .bootstrap()
      .then((data) => {
        setSnapshot(data);
        setSyncState("synced");
      })
      .catch((err) => {
        if (isNetworkError(err)) setSyncState("offline");
        else setLastError(String(err instanceof Error ? err.message : err));
      });
  }, [config]);

  const connect = useCallback(
    async (next: ServerConfig) => {
      const built = createAppApi(next);
      try {
        // 切服务器时清掉旧 snapshot 与写队列，避免两个服务端数据混在一起
        built.engine.clearSnapshot();
        built.engine.clearQueue();
        setSyncState("syncing");
        const data = await built.engine.bootstrap();
        setSnapshot(data);
        apiRef.current = built;
        setApiState(built);
        saveConfig(next);
        setConfigState(next);
        setSyncState("synced");
      } catch (err) {
        setLastError(isNetworkError(err) ? "服务端不可达，检查地址与网络" : String(err instanceof Error ? err.message : err));
        throw err;
      }
    },
    [],
  );

  const probeServer = useCallback(async (baseUrl: string) => {
    const base = baseUrl.replace(/\/+$/, "");
    // 探测阶段还没有令牌，用 public 客户端即可：/health 与 /auth/state 都免鉴权。
    const probe = createApiClient({ baseUrl: base, sessionToken: "" });
    const health = await probe.health();
    if (!health.ok) throw new Error("服务器未就绪");
    const state = await probe.request<{ password_set: boolean }>("/api/v1/auth/state", { public: true });
    return { ok: true, passwordSet: state.password_set };
  }, []);

  const authenticate = useCallback(
    async (baseUrl: string, password: string, mode: "login" | "setup") => {
      const base = baseUrl.replace(/\/+$/, "");
      const probe = createApiClient({ baseUrl: base, sessionToken: "" });
      const path = mode === "setup" ? "/api/v1/auth/setup" : "/api/v1/auth/login";
      const res = await probe.request<{ session_token: string; expires_at: string }>(path, {
        method: "POST",
        public: true,
        body: { password },
      });
      await connect({ baseUrl: base, sessionToken: res.session_token });
    },
    [connect],
  );

  /** 改口令。服务端换完口令就作废旧令牌，所以成功后必须整体重连，
   *  否则用户会被自己刚改的口令锁在外面 —— 这是改密路径最容易漏的一步。 */
  const changePassword = useCallback(
    async (oldPassword: string, newPassword: string) => {
      const current = apiRef.current;
      const cur = config;
      if (!current || !cur) throw new Error("尚未登录");
      const res = await current.client.request<{ session_token: string; expires_at: string }>(
        "/api/v1/auth/password",
        { method: "POST", body: { old_password: oldPassword, new_password: newPassword } },
      );
      await connect({ baseUrl: cur.baseUrl, sessionToken: res.session_token });
    },
    [config, connect],
  );

  const disconnect = useCallback(() => {
    clearConfig();
    apiRef.current?.engine.clearSnapshot();
    apiRef.current?.engine.clearQueue();
    apiRef.current = null;
    setApiState(null);
    setSnapshot(null);
    setConfigState(null);
  }, []);

  const refresh = useCallback(async () => {
    if (!apiRef.current) return;
    setSyncState("syncing");
    try {
      const data = await apiRef.current.engine.bootstrap();
      setSnapshot(data);
      setSyncState("synced");
    } catch (err) {
      if (isNetworkError(err)) setSyncState("offline");
      else setLastError(String(err instanceof Error ? err.message : err));
    }
  }, []);

  const flushNow = useCallback(async () => {
    if (!apiRef.current) return;
    setSyncState("syncing");
    try {
      await apiRef.current.engine.flush();
      setSyncState("synced");
      await refresh();
    } catch (err) {
      if (err instanceof SyncFlushError) setSyncState("offline");
      else if (isNetworkError(err)) setSyncState("offline");
      else setLastError(String(err instanceof Error ? err.message : err));
    }
  }, [refresh]);

  const save = useCallback(
    async (input: SaveInput): Promise<"direct" | "queued" | "error"> => {
      const app = apiRef.current;
      if (!app) throw new Error("未连接服务器");

      const op: WriteOp = {
        op_id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
        entity: input.entity,
        entity_id: input.entity_id,
        client_ts: app.engine.clientTs(),
        patch: input.patch,
      };

      // 直传优先：在线写端点直接 POST
      if (input.entity === "piece_progress" && snapshot) {
        const piece = snapshot.pieces.find((p) => p.id === input.entity_id);
        if (piece) {
          try {
            await app.domain.putPieceProgress(piece.project_id, input.entity_id, {
              last_pos: typeof input.patch.last_pos === "number" ? input.patch.last_pos : 0,
              recited: typeof input.patch.recited === "boolean" ? input.patch.recited : undefined,
              client_ts: op.client_ts,
            });
            setSnapshot((prev) => (prev ? applyPatchToSnapshot(prev, op) : prev));
            setSyncState("synced");
            void refresh();
            return "direct";
          } catch (err) {
            if (!isNetworkError(err)) {
              setLastError(String(err instanceof Error ? err.message : err));
              return "error";
            }
          }
        }
      }
      if (input.entity === "checkin") {
        const items = Array.isArray(input.patch.items_json) ? (input.patch.items_json as string[]) : [];
        try {
          await app.domain.putCheckin({ date: input.entity_id, items });
          setSnapshot((prev) => (prev ? applyPatchToSnapshot(prev, op) : prev));
          setSyncState("synced");
          void refresh();
          return "direct";
        } catch (err) {
          if (!isNetworkError(err)) {
            setLastError(String(err instanceof Error ? err.message : err));
            return "error";
          }
        }
      }
      if (input.entity === "node_mastery") {
        const m = input.patch.mastery;
        if (m === "no" || m === "mid" || m === "yes") {
          try {
            await app.domain.setNodeMastery(input.entity_id, m);
            setSyncState("synced");
            void refresh();
            return "direct";
          } catch (err) {
            if (!isNetworkError(err)) {
              setSyncState("synced");
              setLastError(String(err instanceof Error ? err.message : err));
              return "error";
            }
          }
        }
      }

      // 离线（网络异常）或没有直通端点：入队，联网后按 client_ts 重放
      app.engine.enqueue({ ...input, client_ts: op.client_ts, op_id: op.op_id });
      setSnapshot((prev) => (prev ? applyPatchToSnapshot(prev, op) : prev));
      setSyncState("offline");
      void (async () => {
        try {
          await app.engine.flush();
          setSyncState("synced");
          await refresh();
        } catch (err) {
          if (isNetworkError(err)) setSyncState("offline");
          else setLastError(String(err instanceof Error ? err.message : err));
        }
      })();
      return "queued";
    },
    [snapshot, refresh],
  );

  const clearError = useCallback(() => setLastError(null), []);

  const value: AppContextValue = {
    config,
    api: apiState,
    domain: apiState?.domain ?? null,
    snapshot,
    syncState,
    pending: currentApi?.engine.pendingCount() ?? 0,
    lastError,
    probeServer,
    authenticate,
    connect,
    disconnect,
    changePassword,
    refresh,
    flushNow,
    save,
    clearError,
  };

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp 需在 AppProvider 内使用");
  return ctx;
}
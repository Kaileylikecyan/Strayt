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
  testConnection: (baseUrl: string, token: string) => Promise<void>;
  connect: (config: ServerConfig) => Promise<void>;
  disconnect: () => void;
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

  const testConnection = useCallback(async (baseUrl: string, token: string) => {
    const probe = createAppApi({ baseUrl: baseUrl.replace(/\/+$/, ""), token });
    const health = await probe.client.health();
    if (!health.ok) throw new Error("服务器未就绪");
    if (typeof health.schema_version === "undefined") {
      // /health 不暴露 schema_version 也不影响：能连通即可
    }
  }, []);

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
    testConnection,
    connect,
    disconnect,
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
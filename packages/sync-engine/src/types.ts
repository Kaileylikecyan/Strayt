/** 与服务端 sync 协议对齐的数据形（弱同步：全量拉取 + 写队列重放）。 */

export type Writables =
  | "piece_progress"
  | "pair_edit"
  | "checkin"
  | "plan"
  | "goal"
  | "node_mastery"
  | "node_category";

/** 一条写队列操作（服务端 OpIn）。 */
export interface WriteOp {
  op_id: string;
  entity: Writables;
  entity_id: string;
  /** naive UTC ISO，如 2026-09-29T12:00:00 */
  client_ts: string;
  patch: Record<string, unknown>;
}

export type OpStatus = "applied" | "skipped" | "conflict" | "error";

export interface OpResult {
  op_id: string;
  status: OpStatus;
  reason?: string | null;
  server_updated_at?: string | null;
}

export interface SyncSnapshot {
  schema_version: string;
  server_time: string;
  projects: Array<{
    id: string;
    name: string;
    type: "graph" | "recite";
    api_profile_json?: Record<string, unknown> | null;
    created_at: string;
    updated_at: string;
  }>;
  files: Array<Record<string, unknown>>;
  pieces: Array<{
    id: string;
    project_id: string;
    title: string;
    align_mode: string;
    recited: boolean;
    last_pos: number;
    sort_order: number;
    updated_at: string;
  }>;
  pairs: Array<Record<string, unknown>>;
  goals: Array<Record<string, unknown>>;
  plans: Array<{
    id: string;
    project_id: string;
    title: string;
    due_date: string | null;
    status: "todo" | "doing" | "done";
    sort_order: number;
    target_type?: string | null;
    target_id?: string | null;
    updated_at: string;
  }>;
  checkins: Array<{
    date: string;
    items_json: string[];
    updated_at: string;
  }>;
}

export interface BatchOut {
  results: OpResult[];
  server_time: string;
}

/** 上行/下行两个方向的传输层。由 api-client 适配，避免引擎绑死生成类型。 */
export interface SyncApi {
  bootstrap(): Promise<SyncSnapshot>;
  batch(ops: WriteOp[]): Promise<BatchOut>;
}
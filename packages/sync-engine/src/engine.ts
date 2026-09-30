import type { BatchOut, SyncApi, SyncSnapshot, WriteOp, Writables } from "./types";
import type { KVStorage } from "./storage";

/** 弱同步引擎。职责窄：拉全量、存本地、排离线写、联网重放。 */
export interface SyncEngineConfig {
  api: SyncApi;
  storage: KVStorage;
  /** 时钟源，测试可注入。返回 naive UTC ISO。 */
  now?: () => string;
  /** client_ts 相对时钟源前移的毫秒数，防与服务端落库时间同秒而误判。 */
  clientTsOffsetMs?: number;
  /** 单批上限（服务端 200）。 */
  batchLimit?: number;
  snapshotKey?: string;
  queueKey?: string;
}

export interface EnqueueInput {
  entity: Writables;
  entity_id: string;
  patch: Record<string, unknown>;
  /** 不传则用引擎时钟 + 偏移生成。 */
  client_ts?: string;
  op_id?: string;
}

export interface FlushReport {
  sent: number;
  applied: number;
  skipped: number;
  conflict: number;
  error: number;
  /** 仍留在队列里的 op（status=error，或网络异常时整批未发送）。 */
  remaining: WriteOp[];
}

export class SyncFlushError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SyncFlushError";
  }
}

const DEFAULT_BATCH_LIMIT = 200;
const DEFAULT_OFFSET_MS = 5000;

/** 解析 naive UTC（无时区）ISO，返回毫秒时间戳。 */
function toMs(iso: string): number {
  const hasTz = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  const withZone =
    hasTz || iso.length > 10 ? (hasTz ? iso : `${iso}Z`) : `${iso}T00:00:00Z`;
  return Date.parse(withZone);
}

function toNaiveUtcIso(ms: number): string {
  return new Date(ms).toISOString().slice(0, 19);
}

let opSeq = 0;
/** 生成幂等 op_id（客户端去重 + 服务端自然幂等）。 */
export function generateOpId(): string {
  opSeq = (opSeq + 1) % 100000;
  return `${Date.now().toString(36)}-${opSeq.toString(36)}-${Math.random()
    .toString(36)
    .slice(2, 8)}`;
}

export class SyncEngine {
  private readonly api: SyncApi;
  private readonly storage: KVStorage;
  private readonly now: () => string;
  private readonly offsetMs: number;
  private readonly batchLimit: number;
  private readonly snapshotKey: string;
  private readonly queueKey: string;
  private queueCache: WriteOp[] | null = null;

  constructor(config: SyncEngineConfig) {
    this.api = config.api;
    this.storage = config.storage;
    this.now = config.now ?? (() => toNaiveUtcIso(Date.now()));
    this.offsetMs = config.clientTsOffsetMs ?? DEFAULT_OFFSET_MS;
    this.batchLimit = config.batchLimit ?? DEFAULT_BATCH_LIMIT;
    this.snapshotKey = config.snapshotKey ?? "strayt:snapshot";
    this.queueKey = config.queueKey ?? "strayt:write-queue";
  }

  clientTs(): string {
    return toNaiveUtcIso(toMs(this.now()) + this.offsetMs);
  }

  /** 打开应用时一次全量拉取，覆盖本地 snapshot。 */
  async bootstrap(): Promise<SyncSnapshot> {
    const snapshot = await this.api.bootstrap();
    this.storage.set(this.snapshotKey, JSON.stringify(snapshot));
    return snapshot;
  }

  getSnapshot(): SyncSnapshot | null {
    const raw = this.storage.get(this.snapshotKey);
    if (!raw) return null;
    try {
      return JSON.parse(raw) as SyncSnapshot;
    } catch {
      return null;
    }
  }

  hasSnapshot(): boolean {
    return this.getSnapshot() !== null;
  }

  clearSnapshot(): void {
    this.storage.remove(this.snapshotKey);
  }

  /** 离线/在线写入统一走这里：入队（在线时随后 flush）。 */
  enqueue(input: EnqueueInput): WriteOp {
    const op: WriteOp = {
      op_id: input.op_id ?? generateOpId(),
      entity: input.entity,
      entity_id: input.entity_id,
      client_ts: input.client_ts ?? this.clientTs(),
      patch: input.patch,
    };
    const queue = this.readQueue();
    queue.push(op);
    this.writeQueue(queue);
    return op;
  }

  pendingCount(): number {
    return this.readQueue().length;
  }

  peekQueue(): readonly WriteOp[] {
    return this.readQueue();
  }

  clearQueue(): void {
    this.writeQueue([]);
  }

  /**
   * 联网重放：按 client_ts 升序分页发。
   * 服务端 LWW 裁决：applied/skipped/conflict 都从队列移除（conflict 说明服务端更新，旧写入该丢，
   * 否则会无限重放）；只有 status=error（不可写实体/字段）留队列由调用方决定。
   * 网络失败整批保持原样并抛 SyncFlushError。
   */
  async flush(): Promise<FlushReport> {
    const queue = this.readQueue();
    const empty: FlushReport = { sent: 0, applied: 0, skipped: 0, conflict: 0, error: 0, remaining: [] };
    if (queue.length === 0) return empty;

    const sorted = [...queue].sort((a, b) => a.client_ts.localeCompare(b.client_ts));
    const statusByOp = new Map<string, "error">();

    const count = { applied: 0, skipped: 0, conflict: 0, error: 0 };
    let sent = 0;
    let networkFailed = false;
    let failedChunkStart: number | null = null;

    for (let i = 0; i < sorted.length; i += this.batchLimit) {
      const chunk = sorted.slice(i, i + this.batchLimit);
      let batch: BatchOut;
      try {
        batch = await this.api.batch(chunk);
      } catch {
        networkFailed = true;
        failedChunkStart = i;
        break;
      }
      sent += batch.results.length;
      for (const r of batch.results) {
        if (r.status === "applied") count.applied += 1;
        else if (r.status === "skipped") count.skipped += 1;
        else if (r.status === "conflict") count.conflict += 1;
        else {
          count.error += 1;
          statusByOp.set(r.op_id, "error");
        }
      }
    }

    // 已结清（applied/skipped/conflict）移除；error 的保留；
    // 网络中断时，已发送批里的 error 保留，未发送的（失败批及之后）整批保留
    const kept = sorted.filter(
      (op, idx) =>
        statusByOp.has(op.op_id) || (networkFailed && failedChunkStart !== null && idx >= failedChunkStart),
    );
    const inQueue = this.readQueue();
    const keepIds = new Set(kept.map((o) => o.op_id));
    this.writeQueue(inQueue.filter((o) => keepIds.has(o.op_id)));

    const report: FlushReport = {
      sent,
      applied: count.applied,
      skipped: count.skipped,
      conflict: count.conflict,
      error: count.error,
      remaining: kept,
    };
    if (networkFailed) throw new SyncFlushError("网络或服务端不可达，队列保持原样");
    return report;
  }

  private readQueue(): WriteOp[] {
    if (this.queueCache) return this.queueCache;
    const raw = this.storage.get(this.queueKey);
    if (!raw) return [];
    try {
      this.queueCache = JSON.parse(raw) as WriteOp[];
      return this.queueCache;
    } catch {
      return [];
    }
  }

  private writeQueue(queue: WriteOp[]): void {
    this.queueCache = queue;
    this.storage.set(this.queueKey, JSON.stringify(queue));
  }
}
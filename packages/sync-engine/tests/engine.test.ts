import { describe, expect, it } from "vitest";
import { SyncEngine, SyncFlushError, generateOpId, memoryStorage } from "../src/index";
import type { SyncApi, SyncSnapshot, WriteOp } from "../src/index";

const SNAP: SyncSnapshot = {
  schema_version: "1",
  server_time: "2026-09-29T04:00:00",
  projects: [
    {
      id: "p1",
      name: "导游词",
      type: "recite",
      created_at: "2026-09-01T00:00:00",
      updated_at: "2026-09-01T00:00:00",
    },
  ],
  files: [],
  pieces: [],
  pairs: [],
  goals: [],
  plans: [],
  checkins: [],
};

function makeApi(overrides: Partial<SyncApi> = {}): { api: SyncApi; calls: WriteOp[][] } {
  const calls: WriteOp[][] = [];
  const api: SyncApi = {
    async bootstrap() {
      return SNAP;
    },
    async batch(ops) {
      calls.push([...ops]);
      return {
        server_time: "2026-09-29T04:00:00",
        results: ops.map((o) => ({ op_id: o.op_id, status: "applied" as const })),
      };
    },
    ...overrides,
  };
  return { api, calls };
}

describe("SyncEngine 队列与重放", () => {
  it("enqueue 落盘并可计数", () => {
    const storage = memoryStorage();
    const { api } = makeApi();
    const engine = new SyncEngine({ api, storage });
    expect(engine.pendingCount()).toBe(0);
    engine.enqueue({ entity: "checkin", entity_id: "2026-09-29", patch: { items_json: [] } });
    expect(engine.pendingCount()).toBe(1);
    // 重建实例后队列仍在（持久化）
    const engine2 = new SyncEngine({ api, storage });
    expect(engine2.pendingCount()).toBe(1);
  });

  it("默认 client_ts = 时钟 + 偏移（防同秒误判）", () => {
    const { api } = makeApi();
    const engine = new SyncEngine({
      api,
      storage: memoryStorage(),
      now: () => "2026-09-29T12:00:00",
      clientTsOffsetMs: 5000,
    });
    expect(engine.clientTs()).toBe("2026-09-29T12:00:05");
    const op = engine.enqueue({ entity: "checkin", entity_id: "x", patch: {} });
    expect(op.client_ts).toBe("2026-09-29T12:00:05");
  });

  it("flush 按 client_ts 升序发送，一次全清", async () => {
    const { api, calls } = makeApi();
    const engine = new SyncEngine({ api, storage: memoryStorage() });
    engine.enqueue({ entity: "checkin", entity_id: "b", patch: {}, client_ts: "2026-09-29T10:00:00" });
    engine.enqueue({ entity: "checkin", entity_id: "a", patch: {}, client_ts: "2026-09-29T09:00:00" });
    const report = await engine.flush();
    expect(calls).toHaveLength(1);
    expect(calls[0].map((o) => o.entity_id)).toEqual(["a", "b"]);
    expect(report.applied).toBe(2);
    expect(engine.pendingCount()).toBe(0);
  });

  it("conflict 由服务端裁决并从队列移除（不无限重放）", async () => {
    const { api } = makeApi({
      async batch(ops) {
        return {
          server_time: "2026-09-29T04:00:00",
          results: ops.map((o) => ({
            op_id: o.op_id,
            status: "conflict" as const,
            reason: "服务端版本更新，丢弃本次写入",
          })),
        };
      },
    });
    const engine = new SyncEngine({ api, storage: memoryStorage() });
    engine.enqueue({ entity: "piece_progress", entity_id: "p1", patch: { last_pos: 3 } });
    const report = await engine.flush();
    expect(report.conflict).toBe(1);
    expect(engine.pendingCount()).toBe(0);
  });

  it("status=error 留在队列里（不无限重试，但也不静默丢）", async () => {
    const { api } = makeApi({
      async batch() {
        return {
          server_time: "2026-09-29T04:00:00",
          results: [{ op_id: "bad", status: "error", reason: "字段不可写" }],
        };
      },
    });
    const engine = new SyncEngine({ api, storage: memoryStorage() });
    engine.enqueue({ entity: "checkin", entity_id: "x", patch: {}, op_id: "bad" });
    const report = await engine.flush();
    expect(report.error).toBe(1);
    expect(report.remaining).toHaveLength(1);
    expect(engine.pendingCount()).toBe(1);
  });

  it("按 batchLimit 分页", async () => {
    const { api, calls } = makeApi();
    const engine = new SyncEngine({ api, storage: memoryStorage(), batchLimit: 2 });
    for (let i = 0; i < 5; i += 1) {
      engine.enqueue({ entity: "checkin", entity_id: String(i), patch: {}, client_ts: `2026-09-29T0${i}:00:00` });
    }
    await engine.flush();
    expect(calls).toHaveLength(3);
    expect(calls[0]).toHaveLength(2);
    expect(calls[2]).toHaveLength(1);
    expect(engine.pendingCount()).toBe(0);
  });

  it("网络失败整批保留并抛 SyncFlushError", async () => {
    const { api } = makeApi({
      async batch() {
        throw new Error("network down");
      },
    });
    const engine = new SyncEngine({ api, storage: memoryStorage() });
    engine.enqueue({ entity: "checkin", entity_id: "x", patch: {} });
    await expect(engine.flush()).rejects.toBeInstanceOf(SyncFlushError);
    expect(engine.pendingCount()).toBe(1);
  });

  it("node_mastery（F16）：离线入队、在线重放，patch 保持原样", async () => {
    const { api, calls } = makeApi();
    const engine = new SyncEngine({ api, storage: memoryStorage() });
    engine.enqueue({ entity: "node_mastery", entity_id: "node-a", patch: { mastery: "mid" } });
    expect(engine.pendingCount()).toBe(1);
    const report = await engine.flush();
    expect(report.applied).toBe(1);
    expect(engine.pendingCount()).toBe(0);
    expect(calls[0][0].entity).toBe("node_mastery");
    expect(calls[0][0].entity_id).toBe("node-a");
    expect(calls[0][0].patch).toEqual({ mastery: "mid" });
  });
});

describe("SyncEngine snapshot", () => {
  it("bootstrap 拉全量并覆盖本地；clearSnapshot 清掉", async () => {
    const storage = memoryStorage();
    const { api } = makeApi();
    const engine = new SyncEngine({ api, storage });
    await engine.bootstrap();
    expect(engine.hasSnapshot()).toBe(true);
    expect(engine.getSnapshot()?.projects[0].name).toBe("导游词");
    engine.clearSnapshot();
    expect(engine.hasSnapshot()).toBe(false);
  });

  it("generateOpId 有幂等语义（不重复即可）", () => {
    const ids = new Set(Array.from({ length: 200 }, () => generateOpId()));
    expect(ids.size).toBe(200);
  });
});

describe("flush 部分失败", () => {
  it("先发的批清掉，后断网的批保留", async () => {
    let call = 0;
    const { api } = makeApi({
      async batch(ops) {
        call += 1;
        if (call === 2) throw new Error("断网");
        return { server_time: "2026-09-29T04:00:00", results: ops.map((o) => ({ op_id: o.op_id, status: "applied" })) };
      },
    });
    const engine = new SyncEngine({ api, storage: memoryStorage(), batchLimit: 1 });
    engine.enqueue({ entity: "checkin", entity_id: "a", patch: {}, client_ts: "2026-09-29T10:00:00" });
    engine.enqueue({ entity: "checkin", entity_id: "b", patch: {}, client_ts: "2026-09-29T11:00:00" });
    engine.enqueue({ entity: "checkin", entity_id: "c", patch: {}, client_ts: "2026-09-29T12:00:00" });
    await expect(engine.flush()).rejects.toBeInstanceOf(SyncFlushError);
    // b、c 没发出去，留在队列
    expect(engine.peekQueue().map((o) => o.entity_id)).toEqual(["b", "c"]);
  });
});
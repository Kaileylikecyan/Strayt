/**
 * 背诵单元合成与进度推导（文档 F19.1）。
 *
 * 刻意做成纯函数模块：`block_no` 的分组与 `last_pos` 的推导是这块最容易算错的地方，
 * 必须能脱离 React 单测。它依赖的只有 `PairOut` 的 `seq` / `block_no` 两个字段，
 * 所以服务端契约一改这里就会立刻红。
 */
import type { PairOut } from "@strayt/api-client";

/** 一个背诵单元：一段（按段）或一句（按句）。 */
export type Unit = {
  index: number;
  pairs: PairOut[];
  blockNo: number | null;
  /** 单元内最大的 seq；`last_pos > lastSeq` 即该单元整段完成。 */
  lastSeq: number;
};

/** 背诵粒度：按句 / 按段。按段需要 `pairs.block_no`。 */
export type Granularity = "sentence" | "paragraph";

export function blockOf(pair: PairOut): number | null {
  return pair.block_no ?? null;
}

/**
 * 按 `block_no` 把连续 pair 聚成背诵单元（客户端合成，服务端不做）。
 * 块号必须**连续相同**才合并 —— 拆句/合并句会让块号重复或跳号，
 * 相邻同号仍属于同一段，跳号说明中间被编辑过，按原块分组反而会误导。
 */
export function buildUnits(pairs: PairOut[], granularity: Granularity): Unit[] {
  const units: Unit[] = [];
  for (const pair of pairs) {
    const last = units[units.length - 1];
    const mergeInto =
      granularity === "paragraph" &&
      last !== undefined &&
      last.blockNo !== null &&
      last.blockNo === blockOf(pair);
    if (mergeInto && last !== undefined) {
      last.pairs.push(pair);
      last.lastSeq = Math.max(last.lastSeq, pair.seq);
    } else {
      units.push({
        index: units.length,
        pairs: [pair],
        blockNo: blockOf(pair),
        lastSeq: pair.seq,
      });
    }
  }
  return units;
}

/**
 * 这篇目能不能按段。任一 pair 缺块号就整体退化为按句 ——
 * 只对部分句子分组会让同一段一半按段一半按句，比全按句更糟（文档 F19.1）。
 */
export function paragraphUsable(pairs: PairOut[]): boolean {
  return pairs.length > 0 && pairs.every((p) => blockOf(p) !== null);
}

/**
 * 单元完成度：`last_pos` 是句级进度（= 已背过的句子数），
 * 单元内**每一句**都过线才算整段完成（文档 F19.1）。
 */
export function isUnitDone(unit: Unit, lastPos: number): boolean {
  return unit.pairs.every((p) => p.seq < lastPos);
}

/** 已背过的单元数。 */
export function countDoneUnits(units: Unit[], lastPos: number): number {
  return units.filter((u) => isUnitDone(u, lastPos)).length;
}

/**
 * 第一个没整段背完的单元；全背完时停在最后一个（还要让用户看已揭示的那段）。
 */
export function currentUnitIndex(units: Unit[], lastPos: number): number {
  const i = units.findIndex((u) => !isUnitDone(u, lastPos));
  return i === -1 ? Math.max(units.length - 1, 0) : i;
}

/** 揭示某单元后应落库的 `last_pos`：至少覆盖该单元末句之后。 */
export function lastPosAfterReveal(lastPos: number, unit: Unit): number {
  return Math.max(lastPos, unit.lastSeq + 1);
}

/** `last_pos` 收敛进 [0, pair_count]，避免脏值把进度条顶到界外。 */
export function clampLastPos(lastPos: number | null | undefined, pairCount: number): number {
  if (typeof lastPos !== "number" || !Number.isFinite(lastPos)) return 0;
  return Math.min(Math.max(Math.round(lastPos), 0), pairCount);
}

export const TIMER_MIN_SEC = 10;
export const TIMER_MAX_SEC = 60 * 60;
export const TIMER_FALLBACK_SEC = 180;
export const TIMER_PRESETS = [60, 180, 300];

export function clampSec(n: number): number {
  if (!Number.isFinite(n)) return TIMER_FALLBACK_SEC;
  return Math.min(Math.max(Math.round(n), TIMER_MIN_SEC), TIMER_MAX_SEC);
}

export function formatCountdown(sec: number): string {
  const s = Math.max(sec, 0);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/**
 * 解析就地可编辑的时长输入：`3:00` / `3：00`（全角冒号）/ `180`（纯秒）。
 *
 * 返回 `null` 表示输入**还没成形**（空串、`"3:"`、`"abc"`），调用方要保留旧值
 * 而不是当成 0 —— 否则用户删到一半就会把时长打成最小值。
 * 秒位 ≥60 视为无效（`3:75`），不静默进位。
 */
export function parseClock(text: string): number | null {
  const t = text.trim();
  if (t === "") return null;
  const m = /^(\d{1,3})\s*[:：]\s*(\d{1,2})$/.exec(t);
  if (m) {
    const mm = Number(m[1]);
    const ss = Number(m[2]);
    if (ss > 59) return null;
    return clampSec(mm * 60 + ss);
  }
  if (/^\d+$/.test(t)) return clampSec(Number(t));
  return null;
}

const GRANULARITY_PREFIX = "strayt:recite:granularity:";

export function granularityStorageKey(pieceId?: string): string | null {
  return pieceId ? `${GRANULARITY_PREFIX}${pieceId}` : null;
}

/**
 * 粒度偏好按篇目存在**本地**：不上服务端、不进弱同步队列（ADR-0012）。
 * 换设备 / 清缓存会回到按句，只是偏好丢失，不影响任何学习数据。
 */
export function readGranularity(pieceId?: string): Granularity {
  const key = granularityStorageKey(pieceId);
  if (!key) return "sentence";
  try {
    return window.localStorage.getItem(key) === "paragraph" ? "paragraph" : "sentence";
  } catch {
    return "sentence";
  }
}

export function writeGranularity(pieceId: string | undefined, value: Granularity): void {
  const key = granularityStorageKey(pieceId);
  if (!key) return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* 隐私模式下写不进去就退化成「只按句」，不影响其他功能 */
  }
}
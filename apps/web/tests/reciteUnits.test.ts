import { describe, expect, it } from "vitest";
import type { PairOut } from "@strayt/api-client";
import {
  buildUnits,
  clampLastPos,
  clampSec,
  countDoneUnits,
  currentUnitIndex,
  formatCountdown,
  isUnitDone,
  lastPosAfterReveal,
  paragraphUsable,
  TIMER_FALLBACK_SEC,
  TIMER_MAX_SEC,
  TIMER_MIN_SEC,
} from "../src/pages/reciteUnits";

/** 只填 `reciteUnits` 真正读到的字段，其余按契约补齐。 */
function pair(seq: number, blockNo: number | null): PairOut {
  return {
    pair_key: `p${seq}`,
    piece_id: "piece-1",
    seq,
    zh: `中文${seq}。`,
    en: `english ${seq}.`,
    loc_page: seq + 1,
    flagged_by: null,
    manually_edited: false,
    block_no: blockNo,
  } as PairOut;
}

describe("按句 / 按段合成背诵单元（F19.1）", () => {
  /** 文档验收用例的形状：3 段、每段 3 句，共 9 句。 */
  const nine = [
    pair(0, 1), pair(1, 1), pair(2, 1),
    pair(3, 2), pair(4, 2), pair(5, 2),
    pair(6, 3), pair(7, 3), pair(8, 3),
  ];

  it("按句时一句一单元", () => {
    const units = buildUnits(nine, "sentence");
    expect(units).toHaveLength(9);
    expect(units.every((u) => u.pairs.length === 1)).toBe(true);
  });

  it("按段时连续同块号聚成一段", () => {
    const units = buildUnits(nine, "paragraph");
    expect(units.map((u) => u.pairs.length)).toEqual([3, 3, 3]);
    expect(units.map((u) => u.blockNo)).toEqual([1, 2, 3]);
    expect(units.map((u) => u.index)).toEqual([0, 1, 2]);
  });

  it("块号跳号时不开新段（跳号说明中间被编辑过，按原块分组会误导）", () => {
    const units = buildUnits([pair(0, 1), pair(1, 3)], "paragraph");
    expect(units).toHaveLength(2);
  });

  it("单元 lastSeq 取组内最大 seq", () => {
    const units = buildUnits(nine, "paragraph");
    expect(units.map((u) => u.lastSeq)).toEqual([2, 5, 8]);
  });

  it("空篇目产出零单元，不抛错", () => {
    expect(buildUnits([], "paragraph")).toEqual([]);
  });
});

describe("缺块号时整体降级为按句（F19.1）", () => {
  it("任一 pair 缺块号就不能按段", () => {
    expect(paragraphUsable([pair(0, 1), pair(1, null)])).toBe(false);
    expect(paragraphUsable([pair(0, 1), pair(1, 2)])).toBe(true);
  });

  it("空篇目不算可按段", () => {
    expect(paragraphUsable([])).toBe(false);
  });

  it("按句模式对缺块号的篇目照常工作", () => {
    const units = buildUnits([pair(0, null), pair(1, null)], "sentence");
    expect(units.map((u) => u.blockNo)).toEqual([null, null]);
    expect(units).toHaveLength(2);
  });
});

describe("last_pos 推导单元进度（F19.1）", () => {
  // 3 段共 9 句，段界为 [0..2] [3..5] [6..8]
  const units = buildUnits(
    [
      pair(0, 1), pair(1, 1), pair(2, 1),
      pair(3, 2), pair(4, 2), pair(5, 2),
      pair(6, 3), pair(7, 3), pair(8, 3),
    ],
    "paragraph",
  );

  it("背到第 1 段的第 3 句 → 该段完成、第 2 段未完成、进度 1/3 段", () => {
    const lastPos = 3;
    expect(isUnitDone(units[0], lastPos)).toBe(true);
    expect(isUnitDone(units[1], lastPos)).toBe(false);
    expect(countDoneUnits(units, lastPos)).toBe(1);
  });

  it("差一句没过线就算整段未完成", () => {
    expect(isUnitDone(units[0], 2)).toBe(false);
    expect(isUnitDone(units[0], 3)).toBe(true);
  });

  it("揭示第 2 段后 last_pos 覆盖到该段末句之后", () => {
    expect(lastPosAfterReveal(3, units[1])).toBe(6);
    expect(countDoneUnits(units, lastPosAfterReveal(3, units[1]))).toBe(2);
  });

  it("last_pos 不会因为回退而倒退", () => {
    expect(lastPosAfterReveal(8, units[0])).toBe(8);
  });

  it("当前单元 = 第一个未完成段；全背完停在最后一段", () => {
    expect(currentUnitIndex(units, 0)).toBe(0);
    expect(currentUnitIndex(units, 3)).toBe(1);
    expect(currentUnitIndex(units, 9)).toBe(2);
  });

  it("切换粒度不改变 last_pos 本身（进度与粒度解耦）", () => {
    const lastPos = 3;
    const raw = [pair(0, 1), pair(1, 1), pair(2, 1), pair(3, 2)];
    expect(countDoneUnits(buildUnits(raw, "sentence"), lastPos)).toBe(3);
    expect(countDoneUnits(buildUnits(raw, "paragraph"), lastPos)).toBe(1);
  });

  it("last_pos 脏值收敛进 [0, pair_count]", () => {
    expect(clampLastPos(null, 9)).toBe(0);
    expect(clampLastPos(-5, 9)).toBe(0);
    expect(clampLastPos(999, 9)).toBe(9);
    expect(clampLastPos(3.4, 9)).toBe(3);
  });

  it("全部背完时进度满格", () => {
    expect(countDoneUnits(units, 9)).toBe(3);
  });
});

describe("计时器取值域（F19.3）", () => {
  it("时长收敛进 [10s, 1h]，非法值回落默认", () => {
    expect(clampSec(0)).toBe(TIMER_MIN_SEC);
    expect(clampSec(99999)).toBe(TIMER_MAX_SEC);
    expect(clampSec(Number.NaN)).toBe(TIMER_FALLBACK_SEC);
    expect(clampSec(180.4)).toBe(180);
  });

  it("倒计时按 m:ss 呈现", () => {
    expect(formatCountdown(0)).toBe("0:00");
    expect(formatCountdown(9)).toBe("0:09");
    expect(formatCountdown(185)).toBe("3:05");
    expect(formatCountdown(-5)).toBe("0:00");
  });
});
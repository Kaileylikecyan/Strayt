"""⑥ 通道 B：regular 直配（不用 LLM）。

**为什么不能按句索引配对**：样板实测 12/12 篇中英句数不等（49 vs 55 …），
``zip()`` 会整体错位，而且错位后每对仍是通顺中英文，肉眼完全看不出问题。

算法：单调对齐的**长度平衡 DP**。

把中英两侧各自切成句段后，问题变成「把两侧各自切成**同样多**的组，一一组对」。
约束与代价：

- 单调：第 k 组中英必须按原序对应（不能交叉，译文顺序不会乱序）；
- 代价：``|log(中组长 / 英组长)|`` —— 长度比越接近 1 越好；
- 每组最多并 ``MAX_GROUP`` 句（限制 DP 规模，同时避免把整段糊成一对）。

DP 状态 ``dp[i][j]`` = 吃掉前 i 个中句、前 j 个英句的最小总代价，
``dp[N][M]`` 就是答案。句数不等时，某一侧会多并几句，比例自然被拉平。
"""

from __future__ import annotations

import math

from app.align.base import LOW_CONFIDENCE, Aligner, AlignError, AlignResult, PairDraft

#: 单个组最多并几句。4 足够吸收「中 1 句 ↔ 英 2 句」这类翻译习惯差异，
#: 再大只会让 DP 变慢且容易过拟合噪声。
MAX_GROUP = 4
#: **每多合并一句的罚分**。这是让 DP 产出细粒度对的唯一有效手段。
#:
#: 关键在于：比例一致的分组（1↔1、2↔2、3↔3、4↔4）**代价完全相同** ——
#: 同比例放大不改变长度比。所以任何「按组计」的常数项（无论正负）都只是在
#: 奖励或惩罚组数本身，无法区分细粒度与粗粒度；实测加了「每组固定罚分」后
#: DP 全选 4↔4，把 49+54 句压成 13 对。
#:
#: 按「合并了几句」计费才对。取值必须**大于一次合法 1↔2 的局部长度偏差**，
#: 否则 DP 会为了「比例完美」去并组而不是为了「粒度合适」：
#:
#:     10 中 + 14 英（句长固定，无噪声，最容易退化）
#:       细粒度  6×1:1 + 4×1:2  →  局部 2.02 + 罚 4×P + 局部 1.43
#:       粗粒度  3×4:4          →  局部 0.00 + 罚 14×P
#:       P=0.15 → 3.45 vs 2.10  粗粒度胜（实测只剩 4 对）
#:
#: 0.4 是实测拐点：句长全等的 10/14、14/10、25/31、40/55 四组输入，对数全部
#: **正好等于中文句数**（10/10/25/40），无一退化；样板 12 篇共 626 对、其中
#: 557 对为 1:1、仅 4 对需人工核对。再往上（0.5~0.8）对数不再变好而待核数
#: 翻倍（9→11→13），因为罚分开始压过真实的长度信号，强行拆开本该合并的对。
MERGE_PENALTY = 0.4


class RegularAligner(Aligner):
    name = "regular"

    def align(
        self, zh_segments: list[str], en_segments: list[str], *, loc_page: int
    ) -> AlignResult:
        zh = [s for s in (x.strip() for x in zh_segments) if s]
        en = [s for s in (x.strip() for x in en_segments) if s]
        res = AlignResult(mode=self.name)

        if not zh and not en:
            raise AlignError("empty", "中英两侧都是空的，没有可对齐的内容")
        if not zh or not en:
            res.dropped_zh = zh
            res.dropped_en = en
            res.warnings.append("只有单侧内容，无法配对")
            return self._finalize(res)

        groups = _dp_align(zh, en)
        if groups is None:
            # DP 放弃：退化成「整篇配一对」，至少不丢内容，并明确标记待核对。
            res.pairs = [
                PairDraft(
                    seq=0, zh=" ".join(zh), en=" ".join(en), loc_page=loc_page, confidence=0.2
                )
            ]
            res.warnings.append("长度平衡 DP 未收敛，已退化为整篇一对，需人工核对")
            return self._finalize(res)

        used_zh: set[int] = set()
        used_en: set[int] = set()
        for gi, gj in groups:
            zs = [zh[i] for i in range(gi[0], gi[1] + 1)]
            es = [en[j] for j in range(gj[0], gj[1] + 1)]
            used_zh.update(range(gi[0], gi[1] + 1))
            used_en.update(range(gj[0], gj[1] + 1))
            ztxt = "".join(zs)
            etxt = " ".join(x.strip() for x in es)
            how = "direct"
            conf = 1.0
            if len(zs) > 1 or len(es) > 1:
                how = "merged"
                # 并了几句就有多大概率错位，置信度按并的规模衰减
                conf = 1.0 - 0.12 * ((len(zs) - 1) + (len(es) - 1))
            res.pairs.append(
                PairDraft(
                    seq=len(res.pairs),
                    zh=ztxt,
                    en=etxt,
                    loc_page=loc_page,
                    confidence=max(0.0, round(conf, 3)),
                    how=how,
                )
            )

        res.dropped_zh = [zh[i] for i in range(len(zh)) if i not in used_zh]
        res.dropped_en = [en[j] for j in range(len(en)) if j not in used_en]
        if res.dropped_zh or res.dropped_en:
            res.warnings.append(
                f"有 {len(res.dropped_zh)} 句中文、{len(res.dropped_en)} 句英文未配对"
            )
        res = self._finalize(res)
        # 置信度兜底放在通道 B 这一侧：并组规模越大越可能错位，低于阈值就标待核。
        # 不能挪进 ``_finalize`` —— 通道 A 建对时 confidence 初始化为 0.0
        # （等交叉校验统一赋值），共用的话会把通道 A 的每一对都误判成低置信度。
        for p in res.pairs:
            if p.confidence < LOW_CONFIDENCE:
                p.needs_review = True
                p.flagged_by = p.flagged_by or "merge"
        return res


def _dp_align(zh: list[str], en: list[str]) -> list[tuple[tuple[int, int], tuple[int, int]]] | None:
    """长度平衡 DP。返回分组列表，失败返回 ``None``。

    ``dp[i][j]`` = 覆盖中 ``[0,i)``、英 ``[0,j)`` 的最小总代价。

    代价主项是**归一化局部长度比** ``|log(zsum/esum) - log(ztot/etot)|``。

    实测样板中文句均 40 字、英文句均 130 字（英文句子天生更长：中文导游词爱用
    短句，英文译文会合并），全篇比约 1:3.75。

    - **不归一化**（直接比 ``zsum/esum``）时，``3 中 + 1 英`` 的比≈1 看着最完美，
      DP 会把 49+54 句压成 15 对 —— 等于把三句话糊成一条。
    - **归一化**之后，``1 中 + 1 英`` 的局部比与全篇比只差几个百分点，代价极小；
      真正非比例的 ``1↔2`` 才会被罚。信号方向被纠正，DP 才会给出
      「≈中文句数的细粒度对 + 少数 1:2」。

    注意归一化的**比值方向必须与局部的 ``zsum/esum`` 一致**（都是中/英），
    写成 ``etot/ztot`` 会把除法变成加法，让完美匹配背上最高代价。

    另有一项**低权重进度偏差**（两侧各自归一化的累计占比之差）只用来在
    代价接近时打破平局、保证单调推进。**它绝不能当主项** —— 它是按边界求和
    的，边界越多项越多，天然奖励粗对齐（实测让 DP 全选 ``4↔4``）。

    再加每组固定罚分，避免代价持平时偏向少分组。
    """
    n, m = len(zh), len(en)
    zp = _prefix([len(x) for x in zh])
    ep = _prefix([len(x) for x in en])
    ztot, etot = zp[n], ep[m]
    if ztot == 0 or etot == 0:
        return None
    # 全篇长度比，方向必须与下面 ``zsum / esum`` **一致**（都是中/英）。
    # 写成 ``etot / ztot`` 会把「归一化」变成把两个比相加：
    # ``log(46/166) - log(3.75) = -1.282 - 1.322 = -2.60``，
    # 比例完全正确的 1:1 反而拿到最高代价，DP 只能去并组。（实测踩过。）
    log_overall = math.log(ztot / etot)

    INF = float("inf")
    dp = [[INF] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = INF
            best_prev: tuple[int, int] | None = None
            zf = zp[i] / ztot
            ef = ep[j] / etot
            for ki in range(1, min(MAX_GROUP, i) + 1):
                pi = i - ki
                zsum = zp[i] - zp[pi]
                for kj in range(1, min(MAX_GROUP, j) + 1):
                    pj = j - kj
                    if (pi == 0) != (pj == 0):
                        continue  # 起点必须两侧同时起步
                    prev = dp[pi][pj]
                    if prev == INF:
                        continue
                    esum = ep[j] - ep[pj]
                    local = abs(math.log(zsum / esum) - log_overall)
                    progress = abs(zf - ef)
                    merged = (ki - 1) + (kj - 1)
                    total = prev + local + 0.05 * progress + MERGE_PENALTY * merged
                    if total < best:
                        best = total
                        best_prev = (pi, pj)
            dp[i][j] = best
            back[i][j] = best_prev

    if dp[n][m] == INF:
        return None

    # 回溯
    groups: list[tuple[tuple[int, int], tuple[int, int]]] = []
    i, j = n, m
    while i > 0 or j > 0:
        prev = back[i][j]
        if prev is None:
            return None
        pi, pj = prev
        groups.append(((pi, i - 1), (pj, j - 1)))
        i, j = pi, pj
    groups.reverse()
    return groups


def _prefix(lengths: list[int]) -> list[int]:
    out = [0] * (len(lengths) + 1)
    for i, v in enumerate(lengths):
        out[i + 1] = out[i] + max(1, v)
    return out

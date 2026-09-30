"""⑦ 质量自检：入库前的最后一道闸门。

文档 §6 加工管线第 ⑦ 步的要求是「入库前必过 CJK 比例自检（中文字段 ≈100% 中文、
英文字段 ≈0%），任一篇不过则拦下不入库」。

这里的「拦下」是**整篇**粒度，不是逐对。一篇里有一对串了语言，正确的做法是把整篇
挡在库外并报出位置，而不是让用户自己从 60 对里找出那一对。

四级判定，从轻到重：

===============  ==========================  ==========================
判定              含义                        后果
===============  ==========================  ==========================
``ok``           全部通过                      入库
``degraded``      低于期望，但有可用替代         入库，标记待核 + 告警
``rejected``      内容不合法/失真                整篇不入库
``failed``        自检本身无法执行              整篇不入库
===============  ==========================  ==========================

阈值取自 12 篇真实范文的实测分布（见 ``QUALITY_ZH_MIN`` / ``QUALITY_EN_MAX``
出处），不是拍的。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

from app.align.base import LOW_CONFIDENCE, AlignResult, count_sentences
from app.parse.lang import QUALITY_EN_MAX, QUALITY_ZH_MIN, cjk_ratio

#: 空对占比超过此值视为解析/对齐出了问题（而非单纯资料短）
MAX_EMPTY_RATIO = 0.02
#: 单侧为空的对，超过这个**绝对数**就是硬错误（不论篇章大小）。
#:
#: 一开始只按纯比例卡 0.5%，阈值直接失效：真实篇目只有 45~66 对，任何一对
#: 单侧就占 1.5%~2.2%，必然超限 —— 等于「有一对单侧就整篇拒收」，太脆。资料里
#: 出现一句纯公式/纯符号并不罕见，单个应该放行。真数据实测 634 对里单侧为 0。
MAX_ONE_SIDED_ABS = 3
#: 比例兜底。只在**小篇目**上真正起作用（绝对数更宽松时）：n < 37 时
#: ``MAX_ONE_SIDED_ABS / n > 0.08``，比如 10 对里有 1 对单侧（10%）就报。
MAX_ONE_SIDED_RATIO = 0.08
#: 长度比偏离篇内基准的对占比上限
MAX_RATIO_OUTLIER_RATIO = 0.1
#: 句数比偏离篇内基准的对占比上限
MAX_SENTENCE_SPIKE_RATIO = 0.1
#: 段长过于均匀的下限（``p90 / p50``）。真数据 12 篇实测 ``p90/p50`` ∈
#: [1.43, 1.81]（中位 1.62），这里取 1.25 留约 15% 余量。真正「没按语义切开」
#: 的退化情形会落在 1.05~1.15。
MIN_SPREAD = 1.25
#: 段长跨度过大的上限（``p90 / p10``）。真数据实测 ∈ [2.50, 4.47]（中位 3.0），
#: 取 8.0 留 1.8 倍余量，只兜「主体分布被极端值拉散」的情况。
_MAX_SPREAD = 8.0

#: 「两侧都很短」的门槛（字符）。真数据实测中文侧最短 4 字符（如「谢谢大家！」）、
#: 英文侧最短 14 字符，所以单侧短是正常的；只有**两侧同时**短到这个程度才说明
#: 这一对退化成了碎片。
MIN_LEN = 8


class Verdict(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class Issue:
    """一条自检结论。``seq`` 为 ``None`` 表示篇级问题。"""

    code: str
    message: str
    seq: int | None = None
    #: ``info`` 只展示，``warn`` 标记待核，``error`` 导致整篇拒收
    level: str = "warn"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "seq": self.seq, "level": self.level}


@dataclass
class QualityReport:
    verdict: Verdict
    issues: list[Issue] = field(default_factory=list)
    #: 0~1，取逐对置信度与 CJK 比例的最小值再平均；整体可信度一眼可见
    score: float = 1.0

    @property
    def ok(self) -> bool:
        return self.verdict in (Verdict.OK, Verdict.DEGRADED)

    @property
    def messages(self) -> list[str]:
        return [i.message for i in self.issues]

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    def to_dict(self) -> dict:
        return {
            "verdict": str(self.verdict),
            "score": round(self.score, 4),
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
        }


def _r(value: float, nd: int = 4) -> float:
    """收敛到定长小数。``score``/``agreement`` 要写进 JSON 列，浮点尾数
    会让「重跑结果是否与上次一致」这种比较变得不可靠。"""
    if not math.isfinite(value):
        return 0.0
    return round(value, nd)


def _check_empty(res: AlignResult) -> list[Issue]:
    out: list[Issue] = []
    pairs = res.pairs
    if not pairs:
        return [Issue("no_pairs", "没有任何段落对，无法入库", level="error")]

    both_empty = [p.seq for p in pairs if not p.zh.strip() and not p.en.strip()]
    if both_empty:
        out.append(
            Issue(
                "empty_pair",
                f"{len(both_empty)} 对中英皆空（起始 seq={both_empty[0]}）",
                level="error",
            )
        )
    ratio = len(both_empty) / len(pairs)
    if not both_empty and ratio > MAX_EMPTY_RATIO:
        out.append(
            Issue("empty_ratio", f"空对占比 {ratio:.1%} 超过 {MAX_EMPTY_RATIO:.0%}", level="warn")
        )

    one = [p.seq for p in pairs if bool(p.zh.strip()) != bool(p.en.strip())]
    if one:
        r = len(one) / len(pairs)
        level = "error" if (len(one) > MAX_ONE_SIDED_ABS or r > MAX_ONE_SIDED_RATIO) else "warn"
        out.append(
            Issue(
                "one_sided",
                f"{len(one)} 对只有单侧内容（seq={','.join(map(str, one[:8]))}）",
                level=level,
            )
        )
    return out


def _check_len(res: AlignResult) -> list[Issue]:
    out: list[Issue] = []
    short = [
        p.seq for p in res.pairs if len(p.zh.strip()) < MIN_LEN and len(p.en.strip()) < MIN_LEN
    ]
    if short:
        out.append(
            Issue(
                "too_short",
                f"{len(short)} 对两侧都短于 {MIN_LEN} 字符（seq={short[0]}）",
                level="warn",
            )
        )
    return out


def _check_lang(res: AlignResult) -> list[Issue]:
    """语言串位检测：中文栏里混进英文，或英文栏里混进中文。"""
    out: list[Issue] = []
    bad_zh = [(p.seq, cjk_ratio(p.zh)) for p in res.pairs if cjk_ratio(p.zh) < QUALITY_ZH_MIN]
    bad_en = [(p.seq, cjk_ratio(p.en)) for p in res.pairs if cjk_ratio(p.en) > QUALITY_EN_MAX]

    # 同一对里中英互换 = 通道把 zh/en 写反了，这是硬错误
    swapped = sorted({s for s, _ in bad_zh} & {s for s, _ in bad_en})
    if swapped:
        out.append(
            Issue(
                "swapped",
                f"{len(swapped)} 对中英疑似写反（seq={','.join(map(str, swapped[:8]))}）",
                level="error",
            )
        )
    only_zh = sorted({s for s, _ in bad_zh} - {s for s, _ in bad_en})
    if only_zh:
        worst = min(r for s, r in bad_zh if s in only_zh)
        out.append(
            Issue(
                "zh_field_not_zh",
                f"{len(only_zh)} 对中文栏中文占比过低，最低 {worst:.0%}（seq={only_zh[0]}）",
                level="error",
            )
        )
    only_en = sorted({s for s, _ in bad_en} - {s for s, _ in bad_zh})
    if only_en:
        worst = max(r for s, r in bad_en if s in only_en)
        out.append(
            Issue(
                "en_field_has_zh",
                f"{len(only_en)} 对英文栏混入中文，最高 {worst:.0%}（seq={only_en[0]}）",
                level="error",
            )
        )
    return out


def _check_sentences(res: AlignResult) -> list[Issue]:
    """句数爆炸检测。

    单对句数比远超篇内基准，通常是「一整块没收进去」或「切分切歪」的表现。
    真实样板里最极端的一对是 6 中 ↔ 1 英，比值 6；放到 12 也没意义。
    """
    out: list[Issue] = []
    scored: list[tuple[int, float]] = []
    for p in res.pairs:
        nz, ne = count_sentences(p.zh), count_sentences(p.en)
        if nz and ne:
            scored.append((p.seq, max(nz / ne, ne / nz)))
    if len(scored) < 3:
        return out
    vals = sorted(v for _, v in scored)
    base = vals[len(vals) // 2]
    spikes = [s for s, v in scored if v > max(4.0, base * 3)]
    if spikes and len(spikes) / len(res.pairs) > MAX_SENTENCE_SPIKE_RATIO:
        out.append(
            Issue(
                "sentence_spike",
                f"{len(spikes)} 对句数比超篇内中位数 {base:.1f} 的 3 倍（seq={spikes[0]}）",
                level="warn",
            )
        )
    return out


def _check_ratio(res: AlignResult) -> list[Issue]:
    out: list[Issue] = []
    exp = res.expected_ratio
    if exp <= 0:
        return out
    scored: list[tuple[int, float]] = []
    for p in res.pairs:
        if not p.zh or not p.en:
            continue
        scored.append((p.seq, (len(p.en) / len(p.zh)) / exp))
    if not scored:
        return out
    vals = sorted(v for _, v in scored)
    base = vals[len(vals) // 2]
    if base <= 0:
        return out
    out_idx = [s for s, v in scored if v > max(4.0, base * 3) or v < min(0.25, base / 3)]
    if out_idx and len(out_idx) / len(res.pairs) > MAX_RATIO_OUTLIER_RATIO:
        out.append(
            Issue(
                "length_outlier",
                f"{len(out_idx)} 对长度比偏离篇内基准 {base:.2f} 的 3 倍以上（seq={out_idx[0]}）",
                level="warn",
            )
        )
    return out


def _check_spread(res: AlignResult) -> list[Issue]:
    """篇内长度分布。

    最初这里用 ``min/max`` 判跨度，结果真数据 12/12 全部命中（「9~179 字」）——
    范文里有「上海是中国最大的城市」这种 9 字短句，也有 179 字的长段，20 倍
    跨度完全正常。改用**分位数**看主体分布，只在「几乎所有段一样长」或
    「主体分布被一两条极端值拉散」时才提示。
    """
    lens = sorted(len(p.zh) for p in res.pairs if p.zh.strip())
    if len(lens) < 5:
        return []
    n = len(lens)
    p10, p50, p90 = lens[n // 10], lens[n // 2], lens[(n * 9) // 10]
    if p90 < p50 * MIN_SPREAD:
        return [
            Issue(
                "length_uniform",
                f"篇内长度过于均匀（p50={p50} / p90={p90} 字），分段可能没按语义切开",
                level="warn",
            )
        ]
    if p10 and p90 > p10 * _MAX_SPREAD:
        return [
            Issue(
                "length_spread",
                f"篇内长度跨度异常（p10={p10} / p50={p50} / p90={p90} 字）",
                level="warn",
            )
        ]
    return []


def _check_agreement(res: AlignResult, min_agreement: float) -> list[Issue]:
    if not res.used_llm or res.agreement is None:
        return []
    if res.agreement < min_agreement:
        return [
            Issue(
                "low_agreement",
                f"通道 A/B 一致度 {res.agreement:.2f} 低于 {min_agreement:.2f}",
                level="warn",
            )
        ]
    return []


def _score(res: AlignResult) -> float:
    """整体可信度。

    置信度取 **p10** 而不是 ``min()``：通道 B 的长度比闸门在真数据上就会标出
    5.6% 的对（35/620），拿最小值算，真数据的分数被压到 0.39 —— 一篇 46 对里
    只要有 1 对长度比抖了，整篇就显示「39% 可信」。用 p10 反映主体水平，
    个别可疑对的信息已经由 ``needs_review`` 单独承载。
    """
    if not res.pairs:
        return 0.0
    confs = sorted(p.confidence for p in res.pairs)
    conf = confs[max(0, len(confs) // 10)]
    lang = 1.0
    for p in res.pairs:
        if p.zh.strip():
            lang = min(lang, cjk_ratio(p.zh) / QUALITY_ZH_MIN)
        if p.en.strip():
            lang = min(lang, (1.0 - cjk_ratio(p.en)) / (1.0 - QUALITY_EN_MAX))
    dropped = len(res.dropped_zh) / (len(res.pairs) + len(res.dropped_zh))
    agree = res.agreement if res.agreement is not None else 1.0
    return _r(max(0.0, min(1.0, conf)) * max(0.0, min(1.0, lang)) * (1.0 - dropped) * agree)


def check(res: AlignResult, *, min_agreement: float = 0.8) -> QualityReport:
    """对一篇的对齐结果做入库前自检。

    ``min_agreement`` 沿用通道 A 的下限，保持两处判定一致。
    """
    issues: list[Issue] = []
    issues += _check_empty(res)
    issues += _check_len(res)
    issues += _check_lang(res)
    issues += _check_sentences(res)
    issues += _check_ratio(res)
    issues += _check_spread(res)
    issues += _check_agreement(res, min_agreement)

    if any(p.needs_review for p in res.pairs):
        n = sum(1 for p in res.pairs if p.needs_review)
        issues.append(
            Issue(
                "low_confidence",
                f"{n}/{len(res.pairs)} 对置信度低于 {LOW_CONFIDENCE:.2f}",
                level="warn",
            )
        )
    issues += [Issue("align", m, level="warn") for m in res.warnings]
    if res.dropped_zh:
        issues.append(Issue("dropped_zh", f"丢了 {len(res.dropped_zh)} 段中文", level="error"))
    if res.dropped_en:
        issues.append(Issue("dropped_en", f"丢了 {len(res.dropped_en)} 段英文", level="error"))

    if any(i.level == "error" for i in issues):
        verdict = Verdict.REJECTED
    elif issues:
        verdict = Verdict.DEGRADED
    else:
        verdict = Verdict.OK
    return QualityReport(verdict=verdict, issues=issues, score=_score(res))

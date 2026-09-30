"""⑥ 对齐双通道的公共契约。

**最重要的一条不变量（AGENTS.md §6）：段落级对齐，禁止按句索引配对。**

样板 50 页实测 12/12 篇中英句数都**不相等**::

    area1/fw1  中 49 句  英 55 句
    area1/fw2  中 56 句  英 63 句
    area3/fw2  中 47 句  英 51 句
    area6/fw2  中 66 句  英 75 句

按索引 ``zip()`` 配对必然整体错位：第 50 个中文句会配到第 50 个英文句，
而后者其实是第 45 个中文句的翻译。错位之后每一对都是错的，而且**看起来
完全正常**（都是通顺的中英文），用户背诵时才会发现内容对不上。

所以两条通道都必须在「句数不等」的前提下工作：

- 通道 B（regular 直配）：长度平衡 DP，允许一侧合并多句、另一侧保持单句；
- 通道 A（llm 语义对齐）：让模型显式给出「中句序号 → 英句序号列表」的映射。

两条通道的输出形状一致，⑦ 质量自检才有统一的口径去拦。
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.llm.base import Usage

#: 对齐置信度低于此值 → ``needs_review=True``，客户端提示人工核对
LOW_CONFIDENCE = 0.55
#: 长度比容差倍数。**不能对中英长度比设绝对阈值** —— 样板实测中文句均 40 字、
#: 英文句均 130 字，整篇比约 ``1 : 3.4``（中文导游词爱用短句，英文译文会合并）。
#: 写死 ``0.4~2.5`` 这种绝对区间时，全篇每一对都会被判为「明显错位」。
#: 所以改为**按本篇自身的整体比例自校准**：某一对的 ``英/中`` 长度比偏离
#: 本篇整体比超过该倍数才算异常。这样换语言对、换资料版式都不用改阈值。
#:
#: 基准还要再按**组内句数比**折算。``1 中 ↔ 3 英`` 的组，长度比天然就是整篇比的
#: 3 倍，那是正常翻译行为，不是错位。
#:
#: 2.0 这个值不是拍的，取自样板 620 对的 ``比值/基准`` 实测分布：
#: 中位 0.97、p75 1.08、p90 1.19、p95 1.32、p99 1.93、最大 2.47、最小 0.28。
#: 2.0 落在 p99 与最大值之间，正好只标出「最可疑的那 1%」：
#: 实测通道 B 越界 35/620（5.6%），容差放宽到 2.5 就只剩 5/620（0.8%），
#: 收紧到 1.5 则 78/620 全是 DP 为全局一致做的正常局部次优，误报。
LENGTH_RATIO_TOLERANCE = 2.0
#: 覆盖率低于此值 → 整篇不入库
MIN_COVERAGE = 0.9

#: 中英文句末标点。统计组内句数用。
_ZH_END = "。！？"
_EN_END = ".!?"

#: 「数字 + 句号 + 数字」= 小数点，不是句末。样板中文里满是「6340.5 平方千米」
#: 「占地 2.5 平方公里」，不排掉就会把 1 句算成 2 句，进而把长度比基准
#: （``整篇比 × 英句数/中句数``）压偏，实测 634 对里会多出十几个假告警。
_DECIMAL = re.compile(r"\d[.!?]\d")


def count_sentences(text: str) -> int:
    """数句数，跨中英。至少返回 1。"""
    if not text.strip():
        return 1
    t = _DECIMAL.sub("\x00", text)
    n = sum(t.count(c) for c in _ZH_END) + sum(t.count(c) for c in _EN_END)
    return max(1, n)


@dataclass
class PairDraft:
    """一个待入库的段落对。

    ``loc_page`` 必须有值 —— 二期出处回看靠它，丢了只能重跑全部加工。
    """

    seq: int
    zh: str
    en: str
    loc_page: int
    #: 本对的置信度 0~1
    confidence: float = 1.0
    needs_review: bool = False
    #: 这一对是怎么来的：direct | merged | llm | passthrough
    how: str = "direct"
    #: ``needs_review`` 的**来源**：``length_ratio`` | ``empty`` | ``agreement``。
    #: 通道 A 要靠它区分「长度启发式觉得可疑」和「真的缺内容」：前者在两通道
    #: 一致度高时应当赦免，后者怎么都不能赦免。
    flagged_by: str | None = None

    @property
    def length_ratio(self) -> float:
        """英文长度 / 中文长度。"""
        if not self.zh:
            return 0.0
        return len(self.en) / len(self.zh)


@dataclass
class AlignResult:
    """一篇范文的对齐产物。"""

    pairs: list[PairDraft] = field(default_factory=list)
    mode: str = "regular"  # llm | regular
    warnings: list[str] = field(default_factory=list)
    #: 未被配上的中文/英文片段。留档用，⑦ 会据此判覆盖率
    dropped_zh: list[str] = field(default_factory=list)
    dropped_en: list[str] = field(default_factory=list)
    #: 通道 A 的实际 token 消耗；通道 B 为 ``None``
    usage: Usage | None = None
    #: 本次调用是否真的走了 LLM。``False`` 表示 LLM 失败后降级到通道 B，
    #: 调用方要能区分「模型给的对齐」和「长度算出来的对齐」。
    used_llm: bool = False
    #: 通道 A 与通道 B 的结构一致度 0~1。只有 ``used_llm=True`` 时有值，
    #: 用来给置信度定级：两条独立通道结论一致才是真的可信。
    agreement: float | None = None

    @property
    def coverage(self) -> float:
        """覆盖率 = 配上的对数 / (配上的对数 + 落单中文片段数)。"""
        total = len(self.pairs) + len(self.dropped_zh)
        if not total:
            return 0.0
        return len(self.pairs) / total

    @property
    def expected_ratio(self) -> float:
        """本篇整体的「英/中」长度比，作为单对长度的自校准基准。"""
        zh = sum(len(p.zh) for p in self.pairs)
        en = sum(len(p.en) for p in self.pairs)
        if not zh or not en:
            return 0.0
        return en / zh


class Aligner(ABC):
    """对齐通道。"""

    name: str

    @abstractmethod
    def align(
        self, zh_segments: list[str], en_segments: list[str], *, loc_page: int
    ) -> AlignResult:
        """把中英句段对齐成段落对。失败抛 ``AlignError``。"""

    async def align_async(
        self, zh_segments: list[str], en_segments: list[str], *, loc_page: int
    ) -> AlignResult:
        """异步入口。

        通道 B 是纯 CPU 计算，放线程池里跑，别阻塞事件循环；
        通道 A 覆写本方法直接 await LLM（``LLMProvider.chat`` 是协程，
        不能塞进 ``to_thread``）。
        """
        return await asyncio.to_thread(self.align, zh_segments, en_segments, loc_page=loc_page)

    @staticmethod
    def _finalize(res: AlignResult) -> AlignResult:
        """给每一对打 ``needs_review``。子类实现 ``align`` 后调用。

        长度比判定以 ``res.expected_ratio``（本篇整体比）为基准，并按组内句数比
        折算出该组「应有」的比值，再做倍数判定 —— 详见
        ``LENGTH_RATIO_TOLERANCE`` 的说明。

        这里**只**施加本方法自己的两项判定（缺内容、长度比偏离），标记时同时写
        ``flagged_by``。「置信度低于阈值就标待核」那条兜底不放在这里 —— 通道 A
        建对时 ``confidence`` 初始化为 0.0（等交叉校验统一赋值），放在这里会把
        全部对误判成低置信度。兜底在 ``RegularAligner`` 里，就近处理。
        """
        exp = res.expected_ratio
        for p in res.pairs:
            if not p.zh or not p.en:
                p.needs_review = True
                p.flagged_by = "empty"
                p.confidence = min(p.confidence, 0.3)
                continue
            if exp > 0:
                n_zh = count_sentences(p.zh)
                n_en = count_sentences(p.en)
                baseline = exp * (n_en / n_zh)
                ratio = p.length_ratio
                if (
                    ratio < baseline / LENGTH_RATIO_TOLERANCE
                    or ratio > baseline * LENGTH_RATIO_TOLERANCE
                ):
                    p.needs_review = True
                    p.flagged_by = "length_ratio"
                    p.confidence = min(p.confidence, 0.4)
        for i, p in enumerate(res.pairs):
            p.seq = i
        return res


class AlignError(Exception):
    """对齐失败。"""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(message)

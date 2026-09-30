"""⑥ 通道 A：LLM 语义对齐。

通道 B（``regular.py``）只看得见长度，看不见语义。当「一句中文对应两句英文」
不是因为长度凑巧接近，而是因为英文补了主语、拆了并列结构时，长度信号会失手。
通道 A 让模型显式给出「中句序号 → 英句序号列表」的映射。

**为什么只要编号、不要文本。** 提示词要求「仅基于给定材料」（AGENTS.md §7），
但光靠措辞约束不够 —— 模型改写、补主语、把两句话润色成一句都很常见，而且看起来
完全通顺。这里让模型**只输出编号**：任何编造都表现为编号不合法或覆盖率不足，
可被程序判掉，文本一律从原文取，模型碰不到正文。这样「卡片内容超出原文范围」
这条红线在通道 A 上是结构性成立的，不依赖模型听话。

**和通道 B 交叉校验。** 两条通道互相独立（一个看语义、一个看长度）。结论一致
才给高置信度，不一致就标 ``needs_review``。单看一条通道都不足以定论 —— 长度
通道会因巧合失手，语义通道会偶发漂移。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.align.base import (
    LOW_CONFIDENCE,
    Aligner,
    AlignError,
    AlignResult,
    PairDraft,
    count_sentences,
)
from app.align.regular import RegularAligner
from app.llm.base import BudgetExceeded, ChatMessage, LLMError, LLMProvider, Usage
from app.llm.pricing import estimate_cost, estimate_tokens, estimate_tokens_messages

#: 一次请求里每侧最多放多少句。样板单篇 45~75 句，整篇塞进去会让模型
#: 在长上下文里数错编号；切块后每块编号都小而密，映射明显更准。
MAX_CHUNK = 30
#: 报解析错时重试几次（第二次会把上次的错误一起告诉模型）
MAX_RETRIES = 1

_SYSTEM = """你是中英句段对齐器。用户给你两组已编号的句子（中文一组、英文一组），\
它们的顺序都是原文顺序。

你的唯一任务：输出「中文句子编号 → 对应英文句子编号列表」的映射。

硬性要求：
1. 不得改写、翻译、润色、合并或新增任何句子 —— 你只输出编号，不输出文本。
2. 必须覆盖中文的每一个编号，不许漏、不许跳号。
3. 分组必须按原文顺序，不得乱序。
4. 英文句子按顺序使用，同一句英文不得出现两次。
5. 一个中文句子通常对应 1 个英文句子；但英文为了通顺可能拆句，此时 1 个中文\
对应 2~3 个英文。少数情况下两个中文合起来才对应 1 个英文。
6. 只输出一个 JSON 数组，不要任何解释、前言或代码块以外的文字。

输出格式：
[{"zh": 1, "en": [1]}, {"zh": 2, "en": [2, 3]}, {"zh": 3, "en": [4]}]"""


@dataclass
class LlmAlignerConfig:
    max_chunk: int = MAX_CHUNK
    max_retries: int = MAX_RETRIES
    #: 0 表示不限制。F8 要求超预估上限就中断，别把额度烧光。
    budget_cny: float = 0.0
    #: 与通道 B 的一致度（F1）低于此值 → 整体标 ``needs_review``。
    #: 0.8 是实测分的：样板 12 篇跑「正确映射」一致度 0.93~1.00（中位 0.99），
    #: 人为把粒度粗一倍则掉到 0.64~0.70。0.8 落在两组之间且留足余量。
    min_agreement: float = 0.8


def estimate_alignment_tokens(zh_segments: list[str], en_segments: list[str]) -> tuple[int, int]:
    """F8：预估整篇对齐会花掉的 ``(输入, 输出)`` token 数，不真调模型。

    与 ``_ask_model`` 走**同一套**块切与提示词布局（``_n_chunks`` + ``_split_even``
    + ``_build_prompt``），所以预估反映的是「模型实际会看到什么」。输出侧按
    「每对应一对句子输出约 8 个 token 的编号 JSON」粗估。

    引擎用它把整篇的 ``estimated_cny`` 写进 ``jobs.cost_estimate_json``，供
    任务级「实际花费超过预估 50% 中断」（F8）比对。返回 ``(0, 0)`` 表示单侧
    为空、根本不会调模型。
    """
    zh = [s.strip() for s in zh_segments if s.strip()]
    en = [s.strip() for s in en_segments if s.strip()]
    if not zh or not en:
        return 0, 0
    k = _n_chunks(len(zh), len(en), MAX_CHUNK)
    zh_chunks = _split_even(list(range(len(zh))), k)
    en_chunks = _split_even(list(range(len(en))), k)
    prompt_total = 0
    completion_total = 0
    for i in range(k):
        zs, _off = zh_chunks[i]
        es, _ = en_chunks[i]
        zhb = [zh[j] for j in zs]
        enb = [en[j] for j in es]
        prompt_total += estimate_tokens(_SYSTEM) + estimate_tokens(
            _build_prompt(zhb, enb, block=i + 1)
        )
        completion_total += max(1, (len(zhb) + len(enb)) * 8)
    return prompt_total, completion_total


class LlmAligner(Aligner):
    name = "llm"

    def __init__(
        self,
        provider: LLMProvider,
        config: LlmAlignerConfig | None = None,
        *,
        fallback: Aligner | None = None,
    ) -> None:
        self.provider = provider
        self.config = config or LlmAlignerConfig()
        self.fallback = fallback or RegularAligner()

    # -- 同步入口不可用 -------------------------------------------------
    def align(
        self, zh_segments: list[str], en_segments: list[str], *, loc_page: int
    ) -> AlignResult:
        raise AlignError(
            "needs_async",
            "LLM 通道是协程，请用 await align_async(...)",
        )

    async def align_async(
        self, zh_segments: list[str], en_segments: list[str], *, loc_page: int
    ) -> AlignResult:
        zh = [s for s in (x.strip() for x in zh_segments) if s]
        en = [s for s in (x.strip() for x in en_segments) if s]
        if not zh and not en:
            raise AlignError("empty", "中英两侧都是空的，没有可对齐的内容")

        res = AlignResult(mode=self.name)
        # ``Usage`` 是 frozen 的，累加用局部整数，最后一次性构造
        spent = _TokenSpend()
        try:
            groups = await self._ask_model(zh, en, spent)
        except (LLMError, AlignError) as exc:
            # 降级：加工任务不能因为服务商抽风就整体失败。通道 B 不花钱、不联网。
            reason = getattr(exc, "detail", None) or str(exc)
            fb = self.fallback.align(zh, en, loc_page=loc_page)
            fb.usage = spent.to_usage()
            fb.used_llm = False
            fb.warnings.insert(0, f"LLM 对齐失败，已降级为长度直配：{reason}")
            return fb

        self._build(zh, en, groups, res, loc_page)
        res.usage = spent.to_usage()
        res.used_llm = True

        # 交叉校验：拿通道 B 独立跑一遍，比对分组结构
        ref = self.fallback.align(zh, en, loc_page=loc_page)
        res.agreement = _agreement(res.pairs, ref.pairs)
        res = self._finalize(res)
        # 顺序要紧：``_apply_agreement`` 必须在 ``_finalize` **之后**。
        # ``_build`` 建的每一对 confidence 都是 0.0（等交叉校验统一赋值），
        # 而 ``_finalize`` 会把长度可疑的对压到 0.4 —— 两个都只改部分对，
        # 谁后跑谁说了算。先 ``_finalize`` 定标记，再用一致度统一赋值，
        # 最后按需赦免长度标记。
        _apply_agreement(res, res.agreement, self.config.min_agreement)
        if res.agreement >= self.config.min_agreement:
            # ``_finalize`` 用的是通道 B 标定的长度比容差，而通道 A 的分组粒度
            # 不同（实测真数据上会多标 3 倍：通道 B 5.6% vs 通道 A 17.2%，偏离
            # p99 达 5.1 倍）。一致度够高时，模型分组与长度直配互相印证，
            # 这个证据强于单一长度启发式，赦免长度标记。
            # 缺内容（``empty``）是硬缺陷，不在赦免范围内。
            _pardon_length_flags(res)
        else:
            res.warnings.append(
                f"通道 A 与通道 B 的结构一致度仅 {res.agreement:.0%}，已标记为需人工核对"
            )
        return res

    # -- 调模型 ---------------------------------------------------------
    async def _ask_model(
        self, zh: list[str], en: list[str], spent: _TokenSpend
    ) -> list[tuple[list[int], list[int]]]:
        """分块调用，返回 0 基的 ``(中文下标组, 英文下标组)``。

        块内用局部编号（1 基），回来后加偏移换回全局下标 —— 这样每块的编号
        都短而密，模型不容易数错，同时全局顺序天然单调。

        **两侧必须切成同样多的块。** 直接 ``zip(zh_chunks, en_chunks)`` 会在
        块数不等时静默丢掉短侧多出来的块（样板实测 75 句中文切 3 块、54 句英文
        切 2 块，第 3 块中文就永远不会被处理，对齐结果直接少一截）。所以先算
        出一个两边都装得下的块数 ``K``，再按比例等分。
        """
        groups: list[tuple[list[int], list[int]]] = []
        k = _n_chunks(len(zh), len(en), self.config.max_chunk)
        zh_chunks = _split_even(list(range(len(zh))), k)
        en_chunks = _split_even(list(range(len(en))), k)
        for i, (zs, zoff) in enumerate(zh_chunks):
            es, eoff = en_chunks[i]
            prompt = _build_prompt([zh[j] for j in zs], [en[j] for j in es], block=i + 1)
            self._check_budget(prompt, i + 1)
            for gz, ge in await self._ask_block(prompt, len(zs), len(es), block=i + 1, spent=spent):
                groups.append(([zoff + j for j in gz], [eoff + j for j in ge]))
        return groups

    async def _ask_block(
        self, prompt: str, n_zh: int, n_en: int, *, block: int, spent: _TokenSpend
    ) -> list[tuple[list[int], list[int]]]:
        """问一块，**解析和重试必须在同一个循环里**。

        早期版本把 ``_parse_groups`` 放在 ``_chat`` 外面，于是模型输出坏 JSON 时
        ``_chat`` 正常返回、异常直接飞到降级分支，「带上次错误重问」的逻辑成了
        死代码。解析失败是格式问题，把错误回传给模型通常一次就能修好。
        """
        last: str | None = None
        for attempt in range(self.config.max_retries + 1):
            msgs = [ChatMessage.system(_SYSTEM), ChatMessage.user(prompt)]
            if last is not None:
                msgs.append(
                    ChatMessage.user(
                        f"上一次输出无法解析（{last}）。请重新输出，只输出 JSON 数组，不要任何其他文字。"
                    )
                )
            try:
                res = await self.provider.chat(msgs)
            except LLMError as exc:
                # 只有可重试的瞬时错误才重试；Key 错误/额度不足重试多少次都一样，
                # 直接抛给上层降级到通道 B。
                transient = exc.kind in ("rate_limited", "provider_error", "network")
                if transient and attempt < self.config.max_retries:
                    last = f"{exc.kind}: {exc.detail}"
                    continue
                raise
            spent.add(res.usage)
            try:
                return _parse_groups(res.text, n_zh=n_zh, n_en=n_en, block=block)
            except AlignError as exc:
                if attempt >= self.config.max_retries:
                    raise
                last = f"{exc.kind}: {exc.message}"
        raise AlignError("llm_failed", f"重试 {self.config.max_retries} 次仍失败：{last}")

    def _check_budget(self, prompt: str, block: int) -> None:
        """F8：超预估上限就中断，不烧额度。"""
        if self.config.budget_cny <= 0:
            return
        est = estimate_tokens_messages(prompt, expected_output_chars=2000)
        est_cost = estimate_cost(
            self.provider.name,
            self.provider.model,
            est,
            max(1, 2000 // 2),
        )
        if est_cost.total_cny > self.config.budget_cny:
            raise BudgetExceeded(
                "budget",
                f"第 {block} 块预估花费 {est_cost.total_cny:.4f} 元，"
                f"超过上限 {self.config.budget_cny:.4f} 元",
            )

    # -- 落地 ------------------------------------------------------------
    def _build(
        self,
        zh: list[str],
        en: list[str],
        groups: list[tuple[list[int], list[int]]],
        res: AlignResult,
        loc_page: int,
    ) -> None:
        used_zh: set[int] = set()
        used_en: set[int] = set()
        for gz, ge in groups:
            ztxt = "".join(zh[i] for i in gz)
            etxt = " ".join(en[i] for i in ge)
            used_zh.update(gz)
            used_en.update(ge)
            res.pairs.append(
                PairDraft(
                    seq=len(res.pairs),
                    zh=ztxt,
                    en=etxt,
                    loc_page=loc_page,
                    confidence=0.0,  # 下面按一致度统一给
                    how="llm",
                )
            )
        res.dropped_zh = [zh[i] for i in range(len(zh)) if i not in used_zh]
        res.dropped_en = [en[i] for i in range(len(en)) if i not in used_en]
        if res.dropped_zh or res.dropped_en:
            res.warnings.append(
                f"模型未覆盖全部句子：漏 {len(res.dropped_zh)} 句中文、{len(res.dropped_en)} 句英文"
            )


# ==========================================================================
# 提示词 / 解析 / 校验
# ==========================================================================
class _TokenSpend:
    """可累加的 token 计数器。

    ``Usage`` 是 ``frozen=True``（它要进 checkpoint 存盘，不该被中途改写），
    所以累加走这个可变的小对象，最后一次性 ``to_usage()``。
    """

    __slots__ = ("completion", "prompt")

    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0

    def add(self, u: Usage) -> None:
        self.prompt += u.prompt_tokens
        self.completion += u.completion_tokens

    def to_usage(self) -> Usage:
        return Usage(prompt_tokens=self.prompt, completion_tokens=self.completion)


def _build_prompt(zh: list[str], en: list[str], *, block: int) -> str:
    zh_lines = "\n".join(f"[{i + 1}] {s}" for i, s in enumerate(zh))
    en_lines = "\n".join(f"[{i + 1}] {s}" for i, s in enumerate(en))
    return (
        f"这是第 {block} 块。\n\n"
        f"中文句子（共 {len(zh)} 句）：\n{zh_lines}\n\n"
        f"英文句子（共 {len(en)} 句）：\n{en_lines}\n\n"
        f"请把中文 1~{len(zh)} 全部映射到英文编号，输出 JSON 数组。"
    )


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _parse_groups(
    raw: str, *, n_zh: int, n_en: int, block: int
) -> list[tuple[list[int], list[int]]]:
    """从模型输出里抠出 JSON 并校验。

    校验是硬性的，宁可整块判失败降级到通道 B，也不放行一份半对的映射 ——
    半对的映射比没对齐更糟：它看起来正常，背诵时才暴露。
    """
    text = raw.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    if not text.startswith("["):
        # 模型偶尔在 JSON 前后加话，取第一个 [ 到最后一个 ]
        lo, hi = text.find("["), text.rfind("]")
        if lo < 0 or hi <= lo:
            raise AlignError("llm_bad_json", f"第 {block} 块输出里找不到 JSON 数组：{raw[:120]!r}")
        text = text[lo : hi + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AlignError("llm_bad_json", f"第 {block} 块 JSON 解析失败：{exc}") from exc
    if not isinstance(data, list):
        raise AlignError("llm_bad_json", f"第 {block} 块顶层不是数组")

    groups: list[tuple[list[int], list[int]]] = []
    seen_zh: set[int] = set()
    seen_en: set[int] = set()
    prev_zh = -1
    prev_en = -1
    for item in data:
        gz, ge = _norm_item(item, block=block)
        if not gz or not ge:
            raise AlignError("llm_bad_shape", f"第 {block} 块有分组为空：{item!r}")
        # 越界
        if min(gz) < 0 or max(gz) >= n_zh or min(ge) < 0 or max(ge) >= n_en:
            raise AlignError(
                "llm_out_of_range",
                f"第 {block} 块编号越界（中文 0~{n_zh - 1}、英文 0~{n_en - 1}）：{item!r}",
            )
        # 单调 + 不重复
        if min(gz) <= prev_zh or min(ge) <= prev_en:
            raise AlignError("llm_not_monotonic", f"第 {block} 块分组乱序：{item!r}")
        if seen_zh & set(gz) or seen_en & set(ge):
            raise AlignError("llm_duplicated", f"第 {block} 块句子被重复使用：{item!r}")
        if list(gz) != list(range(min(gz), max(gz) + 1)):
            raise AlignError("llm_not_contiguous", f"第 {block} 块中文分组不连续：{item!r}")
        if list(ge) != list(range(min(ge), max(ge) + 1)):
            raise AlignError("llm_not_contiguous", f"第 {block} 块英文分组不连续：{item!r}")
        if len(gz) > 4 or len(ge) > 4:
            raise AlignError("llm_oversize", f"第 {block} 块分组过大（>{4} 句）：{item!r}")
        seen_zh.update(gz)
        seen_en.update(ge)
        prev_zh = max(gz)
        prev_en = max(ge)
        groups.append((gz, ge))

    if not groups:
        raise AlignError("llm_empty", f"第 {block} 块模型没给映射")
    # 两侧都要**完整覆盖**。只查中文是不够的：模型完全可以把英文句子丢在���外
    # 而 JSON 本身合法、编号单调、还覆盖了全部中文。以前就漏了这一句，
    # 结果「丢了 5 段英文」被当成成功返回，直到 ⑦ 质量闸门才拦下 —— 那时
    # 已经白花了模型调用和 token。丢内容必须在解析层就判定为失败，好走重试
    # 或降级通道 B。
    if seen_zh != set(range(n_zh)) or seen_en != set(range(n_en)):
        miss_zh = sorted(set(range(n_zh)) - seen_zh)
        miss_en = sorted(set(range(n_en)) - seen_en)
        detail = []
        if miss_zh:
            detail.append(f"少了 {len(miss_zh)}/{n_zh} 句中文（首个 {miss_zh[0]}）")
        if miss_en:
            detail.append(f"少了 {len(miss_en)}/{n_en} 句英文（首个 {miss_en[0]}）")
        raise AlignError("llm_incomplete", f"第 {block} 块未覆盖全部句子：{'，'.join(detail)}")
    return groups


def _norm_item(item: Any, *, block: int) -> tuple[list[int], list[int]]:
    """容忍几种常见写法，统一成 ``(中文组, 英文组)``。"""
    if isinstance(item, dict):
        gz = item.get("zh", item.get("zh_indices", item.get("zh_idx")))
        ge = item.get("en", item.get("en_indices", item.get("en_idx")))
    elif isinstance(item, (list, tuple)) and len(item) == 2:
        gz, ge = item
    else:
        raise AlignError("llm_bad_shape", f"第 {block} 块分组格式无法识别：{item!r}")
    if isinstance(gz, int):
        gz = [gz]
    if isinstance(ge, int):
        ge = [ge]
    if not isinstance(gz, list) or not isinstance(ge, list):
        raise AlignError("llm_bad_shape", f"第 {block} 块分组不是编号列表：{item!r}")
    try:
        return sorted(int(x) - 1 for x in gz), sorted(int(x) - 1 for x in ge)
    except (TypeError, ValueError) as exc:
        raise AlignError("llm_bad_shape", f"第 {block} 块编号不是整数：{item!r}") from exc


def _n_chunks(n_zh: int, n_en: int, size: int) -> int:
    """两侧都能装下的最大块数。

    取「按 ``size`` 各自需要的块数」的较小者，再受限于较短一侧的句数（否则
    会出现空块）。至少 1。
    """
    if n_zh == 0 or n_en == 0:
        return 1
    need = max(-(-n_zh // size), -(-n_en // size))  # ceil
    return max(1, min(need, n_zh, n_en))


def _split_even(seq: list[int], k: int) -> list[tuple[list[int], int]]:
    """把下标序列等分成 ``k`` 个连续块，返回 ``(块内下标, 全局起始偏移)``。

    差值最多 1，前面的块多拿。用起始偏移而不是累计游标，块之间不会互相牵连。
    """
    n = len(seq)
    if k <= 1:
        return [(seq, 0)]
    base, extra = divmod(n, k)
    out: list[tuple[list[int], int]] = []
    off = 0
    for i in range(k):
        take = base + (1 if i < extra else 0)
        if take <= 0:  # 句数少于块数，多余的块留空
            out.append(([], off))
            continue
        out.append((seq[off : off + take], off))
        off += take
    return out


# ==========================================================================
# 交叉校验
# ==========================================================================
def _zh_boundaries(pairs: list[PairDraft]) -> tuple[set[int], int]:
    """把一组配对换算成「中文侧的累计切分点」。

    返回 ``(切分点集合, 中文总句数)``。切分点用**累计中文句数**表示，
    所以「第 3 句之后切开」在两条通道里是同一个数，与各自切成多少对无关。
    """
    cuts: set[int] = set()
    acc = 0
    for p in pairs:
        acc += count_sentences(p.zh)
        cuts.add(acc)
    return cuts, acc


def _agreement(llm_pairs: list[PairDraft], reg_pairs: list[PairDraft]) -> float:
    """两条通道在「中英该在哪切」上的一致度，取切分点集合的 F1。

    **必须比切分点，不能比同下标的分组签名。** 早期实现逐下标比较
    ``(中句数, 英句数)``，只要两条通道的对数差一个（实测 48 vs 47），后面
    全部错位，一致度直接掉到 0 —— 明明 47 个切点里有 46 个是对的。

    **必须用 F1，不能只用「LLM 的切点有多少落在 regular 里」。** 只算覆盖率
    的话，通道 A 切得比通道 B 稀反而拿满分：实测通道 A 给 3 个切点
    ``{2,4,6}``、通道 B 给 6 个 ``{1..6}``，A 的切点全落在 B 里 → 覆盖率 1.0，
    等于奖励漏切。F1 同时惩罚多切和少切。

    切分点用**累计中文句数**表示（「第 3 句之后断开」在两条通道里是同一个数），
    所以与各自切成多少对无关。
    """
    llm_cuts, _ = _zh_boundaries(llm_pairs)
    reg_cuts, _ = _zh_boundaries(reg_pairs)
    if not llm_cuts or not reg_cuts:
        return 0.0
    hit = len(llm_cuts & reg_cuts)
    if not hit:
        return 0.0
    precision = hit / len(llm_cuts)
    recall = hit / len(reg_cuts)
    return 2 * precision * recall / (precision + recall)


def _agreement_conf(agreement: float, floor: float) -> float:
    """一致度 → 置信度标量。

    完全一致 1.0；低于 ``floor`` 直接压到 ``LOW_CONFIDENCE`` 以下，触发
    ``needs_review``。中间地带线性插值 —— 一致但不完全一致时给个诚实的中间值，
    比拍脑袋给 0.9 有用。
    """
    if agreement >= 0.999:
        return 1.0
    if agreement <= floor:
        return LOW_CONFIDENCE - 0.05
    span = (agreement - floor) / max(1e-9, 1.0 - floor)
    return LOW_CONFIDENCE + (1.0 - LOW_CONFIDENCE) * span


def _apply_agreement(res: AlignResult, agreement: float, floor: float) -> None:
    """按一致度把每一对的置信度统一赋值。

    通道 A 建对时 ``confidence`` 是 0.0（等交叉校验统一赋值），所以这里必须
    覆盖全部对，不能只动被标记的那些。``flagged_by == "empty"`` 的对跳过 ——
    缺内容是硬缺陷，不能被一致度高掩盖。
    """
    conf = round(_agreement_conf(agreement, floor), 3)
    for p in res.pairs:
        if p.flagged_by == "empty":
            continue
        p.confidence = conf
        if conf < LOW_CONFIDENCE:
            p.needs_review = True
            p.flagged_by = p.flagged_by or "agreement"


def _pardon_length_flags(res: AlignResult) -> None:
    """赦免仅由长度启发式触发的标记。

    置信度已由 ``_apply_agreement`` 按一致度赋过值，这里只清标记。保留 ``empty``
    （缺内容）标记 —— 那是硬缺陷，与一致度无关。
    """
    for p in res.pairs:
        if p.needs_review and p.flagged_by == "length_ratio":
            p.needs_review = False
            p.flagged_by = None

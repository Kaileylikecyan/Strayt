"""F11 图谱抽取：把一篇正文提炼成知识点（节点）与关联（边）。

沿用背诵管线 ⑥ 通道 A 的两条纪律：

1. **文本一律从原文取，模型碰不到正文。** 对齐让模型只输出编号；这里让模型
   输出知识点名、概要、关联依据与**逐字引文**（quote）。引文是可判的 ——
   程序拿它去原文里核对（空白折叠归一化），找不到就是编造，直接弃掉这个节点，
   不依赖模型听话。概要/名称/关联依据虽然由模型生成，但提示词只给这份材料，
   且名称与概要都有长度守卫，超大输出不会把 200 字的 ``nodes.name`` 列打爆
   （和 ADR-0001 同一类隐患，这次在入门处就挡住）。

2. **坏 JSON 要带回错误重问，解析和调用必须在同一个循环里。** 早期版本把
   解析丢到重试循环外，模型输出坏 JSON 时「带上次错误重新问」成了死代码。
   解析失败是格式问题，回传错误通常一次就能修好。

字段长度守卫是对「LLM 输出无界」这一事实的防御：模型名字超长、概要超长、
分类名超长都很常见（尤其新模型话痨时），直接在库里炸 ``DataError``（1406）
不如在抽取器里截断并留告警 —— 加工任务不该因为模型啰嗦一个词就整套失败。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.llm.base import (
    BudgetExceeded,
    ChatMessage,
    ChatResult,
    LLMError,
    LLMProvider,
    Usage,
)
from app.llm.pricing import estimate_cost, estimate_tokens_messages

#: 一篇正文最多抽几个知识点。一般资料一篇 3~15 个足够，给 40 是防御模型话痨。
MAX_NODES_PER_UNIT = 40
#: 名称/分类名/概要/引文/关联依据的长度上限。对应 ``nodes.name``(200)、
#: ``categories.name``(120) 两类 NOT NULL 有长度列，其余都是 TEXT 列
#: （超长只影响体验，不炸库）。超长统一**截断 + 告警**，不静默丢弃。
MAX_NAME_CHARS = 200
MAX_CATEGORY_CHARS = 120
MAX_SUMMARY_CHARS = 600
MAX_QUOTE_CHARS = 800
MAX_REASON_CHARS = 200
#: 一段材料最多送多少段给模型。超过就截断，防止长文直接撑爆上下文。
MAX_PARAGRAPHS_PER_PROMPT = 120
#: 报解析错时重试几次（第二次会把上次的错误一起告诉模型）
MAX_RETRIES = 1

_SYSTEM = """你是知识图谱抽取器。用户给你某学习材料的一篇正文（中文为主），\
你的唯一任务是从这篇正文里抽取知识点，并建立知识点之间的关联。

硬性要求：
1. 只使用给定材料的内容。材料里没有的信息一律不写；不确定宁可少抽也不要编造。
2. 每个知识点：
   - name：知识点名，8~20 个字，要有信息量（不要长句、不要带标题编号）；
   - category_index：该知识点所属一级分类在本篇 categories 列表中的下标（从 0 起）；
   - weight：重要度 1~3，3 最重要，材料重点讲的就给高重要度；
   - summary：用 1~2 句概括这个知识点，必须仅基于材料原文；
   - quote：材料里支撑这个知识点的原句，必须逐字照抄材料原文（含标点符号），\
不得改写、拼接或翻译；
   - links：与这个知识点相关的其他知识点，用 index 指本列表里的其他知识点，\
每一条给一个 reason（依据，一句话说明为什么相关，也必须来自材料上下文）。
3. categories 数组：一篇正文通常正好 1 个分类（本篇的章/节名）；只有材料在本篇内\
确实有可明确划分的子主题时，才可以给出多个。数组内名称必须唯一。
4. 只输出 JSON，不要任何解释、前言或代码块以外的文字。"""

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


class _Spend:
    """可累加的 token 计数器。``Usage`` 是 frozen，累加走它，最后一次性封存。"""

    __slots__ = ("completion", "prompt")

    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0

    def add(self, u: Usage) -> None:
        self.prompt += u.prompt_tokens
        self.completion += u.completion_tokens

    def to_usage(self) -> Usage:
        return Usage(prompt_tokens=self.prompt, completion_tokens=self.completion)


class GraphError(Exception):
    """抽取失败。``kind`` 供上层映射成 ``failed_units`` 的 reason。"""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(message)


@dataclass
class GraphNodeDraft:
    """一个待预览的知识点。``links`` 是 ``(本列表下标, 关联依据)`` 列表。"""

    name: str
    weight: int
    category_index: int
    summary: str
    quote: str
    links: list[tuple[int, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "weight": self.weight,
            "category_index": self.category_index,
            "summary": self.summary,
            "quote": self.quote,
            "links": [{"index": i, "reason": r} for i, r in self.links],
        }

    @classmethod
    def from_dict(cls, data: dict) -> GraphNodeDraft:
        links = [
            (int(e["index"]), str(e.get("reason", "")))
            for e in data.get("links", [])
            if isinstance(e, dict) and e.get("index") is not None
        ]
        return cls(
            name=str(data.get("name", "")),
            weight=int(data.get("weight", 2)),
            category_index=int(data.get("category_index", 0)),
            summary=str(data.get("summary", "")),
            quote=str(data.get("quote", "")),
            links=links,
        )


@dataclass
class GraphUnit:
    """一篇正文的抽取产物。``nodes`` 内的 ``category_index`` 是**本篇内**坐标。"""

    title: str
    loc_page: int
    categories: list[str]
    nodes: list[GraphNodeDraft]
    warnings: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "loc_page": self.loc_page,
            "categories": list(self.categories),
            "nodes": [n.to_dict() for n in self.nodes],
            "warnings": list(self.warnings),
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> GraphUnit:
        return cls(
            title=str(data.get("title", "")),
            loc_page=int(data.get("loc_page", 0)),
            categories=[str(c) for c in data.get("categories", [])],
            nodes=[
                GraphNodeDraft.from_dict(n) for n in data.get("nodes", []) if isinstance(n, dict)
            ],
            warnings=[str(w) for w in data.get("warnings", [])],
            usage=Usage(
                prompt_tokens=int(data.get("usage", {}).get("prompt_tokens", 0)),
                completion_tokens=int(data.get("usage", {}).get("completion_tokens", 0)),
            ),
        )


class GraphExtractor:
    """一家的咨询调用封装。``budget_cny`` 为 0 表示不限制。"""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        budget_cny: float = 0.0,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.provider = provider
        self.budget_cny = budget_cny
        self.max_retries = max_retries

    async def extract_async(self, text: str, *, title: str, loc_page: int) -> GraphUnit:
        """抽取一篇。失败抛 ``GraphError`` / ``LLMError``。"""
        paragraphs = _split_paragraphs(text)
        if not paragraphs:
            raise GraphError("empty", f"正文为空：{title}")
        prompt = _build_prompt(title, paragraphs)
        self._check_budget(prompt)
        # ``Usage`` 是 frozen 的，重试也要记账（改版后的对齐管线就是这么算的），
        # 累加走可变计数器，最后一次调用成功后再一次性封存。
        spent = _Spend()
        raw = await self._ask_model(prompt, spent)
        data = parse_json(raw.text)
        return _validate(data, title=title, loc_page=loc_page, source=text, usage=spent.to_usage())

    # -- 调用与重试 -------------------------------------------------------
    async def _ask_model(self, prompt: str, spent: _Spend) -> ChatResult:
        last: str | None = None
        for attempt in range(self.max_retries + 1):
            msgs = [ChatMessage.system(_SYSTEM), ChatMessage.user(prompt)]
            if last is not None:
                msgs.append(
                    ChatMessage.user(
                        f"上一次输出无法解析（{last}）。请重新输出，只输出 JSON，不要任何其他文字。"
                    )
                )
            try:
                res = await self.provider.chat(msgs)
            except LLMError as exc:
                transient = exc.kind in ("rate_limited", "provider_error", "network")
                if transient and attempt < self.max_retries:
                    last = f"{exc.kind}: {exc.detail}"
                    continue
                raise
            spent.add(res.usage)
            # 解析失败是格式问题：把错误回传给模型，通常一次就能修好。
            # 解析和重试必须在同一个循环里，否则这条路径就是死代码。
            try:
                parse_json(res.text)
            except GraphError:
                if attempt >= self.max_retries:
                    raise
                last = "JSON 不是合法对象"
                continue
            return res
        raise GraphError("llm_failed", f"重试 {self.max_retries} 次仍失败：{last}")

    def _check_budget(self, prompt: str) -> None:
        """F8：超预估上限就中断，不烧额度。"""
        if self.budget_cny <= 0:
            return
        est = estimate_tokens_messages(prompt, expected_output_chars=2000)
        est_cost = estimate_cost(self.provider.name, self.provider.model, est, max(1, 2000 // 2))
        if est_cost.total_cny > self.budget_cny:
            raise BudgetExceeded(
                "budget",
                f"预估花费 {est_cost.total_cny:.4f} 元，超过上限 {self.budget_cny:.4f} 元",
            )


def _split_paragraphs(text: str) -> list[str]:
    """段级切分。先按空行，块内再补一刀。与 ⑤ 保持同一套切分口径。"""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _build_prompt(title: str, paragraphs: list[str]) -> str:
    if len(paragraphs) > MAX_PARAGRAPHS_PER_PROMPT:
        paragraphs = paragraphs[:MAX_PARAGRAPHS_PER_PROMPT]
    lines = "\n".join(f"[{i}] {p}" for i, p in enumerate(paragraphs))
    return f"标题：{title}\n\n正文（共 {len(paragraphs)} 段）：\n{lines}"


#: 中文引号、全角标点、空格都归一化，做引文核对时用；折叠空白是第一步，
#: 全角/半角差异是第二步，两层都不命中才判为「原文里找不到」。
_PUNCT = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "！": "!",
        "？": "?",
        "：": ":",
        "；": ";",
        "（": "(",
        "）": ")",
        "“": '"',
        "”": '"',
        "　": " ",
        "—": "-",
        "…": "...",
    }
)


def _fold_ws(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _fold_all(s: str) -> str:
    return _fold_ws(s).translate(_PUNCT)


def _validate(data: dict, *, title: str, loc_page: int, source: str, usage: Usage) -> GraphUnit:
    """把模型 JSON 校验成 ``GraphUnit``。

    校验是**放映灯**：逐字引文必须能在原文里找到；找不到的节点丢弃并留告警。
    默认标注、也允许返回 ``nodes`` 为空的解析上做。

    返回节点数可能少于模型给的，也可能为 0 —— 上层（加工任务）据此走
    「节点全丢 → 本篇失败可续跑」。
    """
    warnings: list[str] = []
    categories, cat_warns = _normalize_categories(data.get("categories"))
    warnings.extend(cat_warns)

    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list):
        raw_nodes = []
    # 第一遍：建「可用节点」。links 引的是**模型给的原始序号**，而节点经过丢弃后
    # 序号会变 —— 所以先把每个可用节点的原始下标记下来，第二遍按 raw→final 映射
    # 翻译关联，落到被丢弃节点上的关联一并丢弃。
    nodes: list[GraphNodeDraft] = []
    raw_to_final: dict[int, int] = {}
    dropped: list[str] = []
    for raw_idx, item in enumerate(raw_nodes):
        if not isinstance(item, dict):
            dropped.append(f"#{raw_idx} 不是对象")
            continue
        name = (item.get("name") or "").strip()
        if not name:
            dropped.append(f"#{raw_idx} 缺名称")
            continue
        if len(name) > MAX_NAME_CHARS:
            warnings.append(f"节点「{name[:20]}…」名称超长，截断为 {MAX_NAME_CHARS} 字")
            name = name[:MAX_NAME_CHARS]

        summary = (item.get("summary") or "").strip()
        if not summary:
            dropped.append(f"#{raw_idx}「{name[:20]}」缺概要")
            continue
        if len(summary) > MAX_SUMMARY_CHARS:
            warnings.append(f"「{name[:20]}」概要超长，截断为 {MAX_SUMMARY_CHARS} 字")
            summary = summary[:MAX_SUMMARY_CHARS]

        quote = (item.get("quote") or "").strip()
        if not quote:
            dropped.append(f"#{raw_idx}「{name[:20]}」缺引文")
            continue
        if not _in_source(quote, source):
            warnings.append(f"「{name[:20]}」的引文未能在原文中找到，已弃掉该节点")
            dropped.append(f"#{raw_idx} 引文对不上原文")
            continue
        if len(quote) > MAX_QUOTE_CHARS:
            warnings.append(f"「{name[:20]}」引文超长，截断为 {MAX_QUOTE_CHARS} 字")
            quote = quote[:MAX_QUOTE_CHARS]

        weight = _clamp_weight(item.get("weight"))
        cat_idx = item.get("category_index")
        if not isinstance(cat_idx, int) or not (0 <= cat_idx < len(categories)):
            warnings.append(f"「{name[:20]}」的分类下标 {cat_idx!r} 越界，归入第 0 类")
            cat_idx = 0
        raw_to_final[raw_idx] = len(nodes)
        nodes.append(
            GraphNodeDraft(
                name=name, weight=weight, category_index=cat_idx, summary=summary, quote=quote
            )
        )

    # 第二遍：关联。以**原始序号**为坐标系防御，再翻译成 final 序号。
    for raw_idx, item in enumerate(raw_nodes):
        if raw_idx not in raw_to_final:
            continue  # 该原始节点已被丢弃
        fin = raw_to_final[raw_idx]
        links = _clean_links(
            item.get("links"),
            valid_count=len(raw_nodes),
            self_index=raw_idx,
            warnings=warnings,
            name=nodes[fin].name,
        )
        translated: list[tuple[int, str]] = []
        for target_raw, reason in links:
            target_final = raw_to_final.get(target_raw)
            if target_final is None:
                continue  # 关联的节点已被丢弃
            translated.append((target_final, reason))
        nodes[fin].links = translated
    if dropped:
        warnings.append(f"共丢弃 {len(dropped)} 个不可用的知识点：{'；'.join(dropped[:8])}")
    return GraphUnit(
        title=title,
        loc_page=loc_page,
        categories=categories,
        nodes=nodes,
        warnings=warnings,
        usage=usage,
    )


def _normalize_categories(raw: Any) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    if not isinstance(raw, list):
        return ["未分类"], [*warnings, "模型未给出分类，回退为「未分类」"]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = (item.get("name") if isinstance(item, dict) else str(item)).strip()
        if not name:
            continue
        if name in seen:
            warnings.append(f"分类「{name}」重复，已去重")
            continue
        seen.add(name)
        if len(name) > MAX_CATEGORY_CHARS:
            warnings.append(f"分类「{name[:20]}…」超长，截断为 {MAX_CATEGORY_CHARS} 字")
            name = name[:MAX_CATEGORY_CHARS]
        out.append(name)
    if not out:
        warnings.append("模型未给出可用分类，回退为「未分类」")
        return ["未分类"], warnings
    return out, warnings


def _clamp_weight(v: Any) -> int:
    try:
        w = int(v)
    except (TypeError, ValueError):
        return 2
    return 2 if w not in (1, 3) else w


def _clean_links(
    raw: Any,
    *,
    valid_count: int,
    self_index: int,
    warnings: list[str],
    name: str,
) -> list[tuple[int, str]]:
    if not isinstance(raw, list):
        return []
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if not isinstance(idx, int) or not (0 <= idx < valid_count) or idx == self_index:
            warnings.append(f"「{name[:20]}」的第 {i} 条关联下标无效，已丢弃")
            continue
        if idx in seen:
            continue
        seen.add(idx)
        reason = (item.get("reason") or "").strip()
        if not reason:
            warnings.append(f"「{name[:20]}」第 {i} 条关联缺依据，已丢弃")
            continue
        if len(reason) > MAX_REASON_CHARS:
            reason = reason[:MAX_REASON_CHARS]
        out.append((idx, reason))
    return out


def _in_source(quote: str, source: str) -> bool:
    src = _fold_ws(source)
    if _fold_ws(quote) in src:
        return True
    return _fold_all(quote) in _fold_all(source)


#: 包 JSON 的 fenced block、前后解释文字都兼容。
def parse_json(raw: str) -> dict:
    """从模型输出里抠出 JSON 并做基本形状校验。"""
    text = raw.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    lo, hi = text.find("{"), text.rfind("}")
    if lo < 0 or hi <= lo:
        raise GraphError("llm_bad_json", f"输出里找不到 JSON 对象：{raw[:120]!r}")
    text = text[lo : hi + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GraphError("llm_bad_json", f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise GraphError("llm_bad_json", "顶层不是对象")
    return data


__all__ = [
    "MAX_NODES_PER_UNIT",
    "GraphError",
    "GraphExtractor",
    "GraphNodeDraft",
    "GraphUnit",
    "parse_json",
]

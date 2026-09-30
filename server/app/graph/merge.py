"""F11 预览态：把各篇的 ``GraphUnit`` 合并成一份可确认的图谱草稿。

抽取按篇为单位各调一次模型，产出的是**本篇内**坐标（``category_index`` 是篇内
下标、``links`` 是篇内节点下标）。合并把它翻译成「项目内全局」：

- 分类：跨篇按名称去重（空白折叠归一化后再比），分配稳定的 ``c000`` key；
- 节点：``u{篇序号}:n{篇内序号}`` 稳定 key —— 确认时的删/改/调都拿它当参照；
- 边：只支持**篇内**关联（一期口径，模型只在单篇上下文里建边）。抽取器已经
  把链接坐标翻译成篇内 final 下标，合并时再翻译成全局 key；目标节点被丢弃的
  关联自然落空，合并后不留死边。

草稿独立于正式表存在（``jobs.results_json``），确认后再原子落库（``persist``）。
预览确认可做三种编辑（对应 F12 的手动调整与 F14 的有限手动编辑）：
删误抽节点、调整分类归属、改名称/概要。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.graph.extract import MAX_NAME_CHARS, MAX_SUMMARY_CHARS, GraphUnit


@dataclass
class GraphDraftCategory:
    key: str
    name: str
    sort_order: int

    def to_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "sort_order": self.sort_order}

    @classmethod
    def from_dict(cls, data: dict) -> GraphDraftCategory:
        return cls(
            key=str(data["key"]),
            name=str(data["name"]),
            sort_order=int(data.get("sort_order", 0)),
        )


@dataclass
class GraphDraftNode:
    key: str
    name: str
    weight: int
    summary: str
    quote: str
    loc_page: int
    category_key: str
    #: 篇内边：[(目标节点 key, 关联依据)]
    links: list[tuple[str, str]] = field(default_factory=list)
    edited: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "weight": self.weight,
            "summary": self.summary,
            "quote": self.quote,
            "loc_page": self.loc_page,
            "category_key": self.category_key,
            "links": [{"to": to, "reason": r} for to, r in self.links],
            "edited": self.edited,
        }

    @classmethod
    def from_dict(cls, data: dict) -> GraphDraftNode:
        links = [
            (str(e["to"]), str(e.get("reason", "")))
            for e in data.get("links", [])
            if isinstance(e, dict) and e.get("to")
        ]
        return cls(
            key=str(data["key"]),
            name=str(data["name"]),
            weight=int(data.get("weight", 2)),
            summary=str(data.get("summary", "")),
            quote=str(data.get("quote", "")),
            loc_page=int(data.get("loc_page", 0)),
            category_key=str(data["category_key"]),
            links=links,
            edited=bool(data.get("edited", False)),
        )


@dataclass
class GraphDraft:
    categories: list[GraphDraftCategory] = field(default_factory=list)
    nodes: list[GraphDraftNode] = field(default_factory=list)
    #: 已确认边 = [(from_key, to_key, reason)]
    edges: list[tuple[str, str, str]] = field(default_factory=list)
    unit_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "unit_count": self.unit_count,
            "warnings": list(self.warnings),
            "categories": [c.to_dict() for c in self.categories],
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [{"from": f, "to": t, "reason": r} for f, t, r in self.edges],
        }

    @classmethod
    def from_dict(cls, data: dict) -> GraphDraft:
        return cls(
            unit_count=int(data.get("unit_count", 0)),
            warnings=[str(w) for w in data.get("warnings", [])],
            categories=[GraphDraftCategory.from_dict(c) for c in data.get("categories", [])],
            nodes=[GraphDraftNode.from_dict(n) for n in data.get("nodes", [])],
            edges=[
                (str(e["from"]), str(e["to"]), str(e.get("reason", "")))
                for e in data.get("edges", [])
                if isinstance(e, dict) and e.get("from") and e.get("to")
            ],
        )


def _fold(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _cat_key(draft: GraphDraft, name: str) -> str:
    """按名称找已有分类；没有则新建。空白折叠归一化后再比。"""
    folded = _fold(name)
    for c in draft.categories:
        if _fold(c.name) == folded:
            return c.key
    key = f"c{len(draft.categories):03d}"
    draft.categories.append(
        GraphDraftCategory(key=key, name=name, sort_order=len(draft.categories))
    )
    return key


def merge_units(units: list[tuple[int, GraphUnit]]) -> GraphDraft:
    """按篇序把抽取产物合成项目级草稿。"""
    draft = GraphDraft(unit_count=len(units))
    for unit_index, unit in units:
        if not unit.nodes:
            continue
        cat_names = unit.categories or ["未分类"]
        local_keys: list[str] = []
        # 第一遍：建节点，占好全局 key。key 里的 ``n索引`` 是**篇内**序号
        # （跨篇按 ``u{篇序}`` 前缀区分），不能用 ``len(draft.nodes)``——
        # 那会在第二篇续排 n02/n03，key 随篇序漂移，预览确认就找不到节点了。
        for i, n in enumerate(unit.nodes):
            cat_name = (
                cat_names[n.category_index] if n.category_index < len(cat_names) else "未分类"
            )
            key = f"u{unit_index:03d}:n{i:02d}"
            draft.nodes.append(
                GraphDraftNode(
                    key=key,
                    name=n.name,
                    weight=n.weight,
                    summary=n.summary,
                    quote=n.quote,
                    loc_page=unit.loc_page,
                    category_key=_cat_key(draft, cat_name),
                )
            )
            local_keys.append(key)
        # 第二遍：回填边（抽取器已把链接翻译成篇内 final 下标）
        for i, n in enumerate(unit.nodes):
            if not n.links:
                continue
            target = local_keys[i]
            links = [(local_keys[t], reason) for t, reason in n.links if 0 <= t < len(local_keys)]
            node = next(x for x in draft.nodes if x.key == target)
            node.links = links

    _prune_empty_categories(draft)
    draft.edges = edges_from_nodes(draft.nodes)
    return draft


def _prune_empty_categories(draft: GraphDraft) -> None:
    used = {n.category_key for n in draft.nodes}
    kept = [c for c in draft.categories if c.key in used]
    for i, c in enumerate(kept):
        c.sort_order = i
    removed = len(draft.categories) - len(kept)
    if removed:
        draft.warnings.append(f"{removed} 个没有知识点的分类已移除")
    draft.categories = kept


def edges_from_nodes(nodes: list[GraphDraftNode]) -> list[tuple[str, str, str]]:
    """从节点 links 重建边：双向去重、自环丢弃、目标必须存在。"""
    node_keys = {n.key for n in nodes}
    edges: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for n in nodes:
        for to_key, reason in n.links:
            if to_key not in node_keys or to_key == n.key:
                continue
            pair = (n.key, to_key)
            if pair in seen or (to_key, n.key) in seen:
                continue
            seen.add(pair)
            edges.append((n.key, to_key, reason))
    return edges


def apply_edits(
    draft: GraphDraft,
    *,
    delete_keys: set[str] | None = None,
    reassign: dict[str, str] | None = None,
    edits: dict[str, dict] | None = None,
) -> tuple[GraphDraft, list[str]]:
    """预览确认前应用手动调整，返回 ``(新草稿, 告警)``。

    - ``delete_keys``：删除误抽的节点（连同指向它/自它的边）；
    - ``reassign``：调整分类归属 ``{节点key: 分类key}``；
    - ``edits``：改名称/概要 ``{节点key: {"name"?, "summary"?}}``，
      改过的节点 ``edited=True``（与 AI 内容分区展示，AGENTS §7）。

    空名称/概要视为删除该节点；没有任何节点的分类一并清掉。
    """
    warnings: list[str] = []
    delete_keys = set(delete_keys or {})
    reassign = reassign or {}
    edits = edits or {}

    valid_keys = {n.key for n in draft.nodes}
    unknown = (delete_keys | set(reassign) | set(edits)) - valid_keys
    if unknown:
        warnings.append(f"以下节点 key 不存在，已忽略：{sorted(unknown)[:5]}")
        delete_keys -= unknown
        reassign = {k: v for k, v in reassign.items() if k in valid_keys}
        edits = {k: v for k, v in edits.items() if k in valid_keys}

    cat_keys = {c.key for c in draft.categories}
    fallback_cat = draft.categories[0].key if draft.categories else None

    nodes: list[GraphDraftNode] = []
    for n in draft.nodes:
        if n.key in delete_keys:
            warnings.append(f"已删除节点「{n.name}」")
            continue
        name, summary = n.name, n.summary
        edited = False
        if n.key in edits:
            edit = edits[n.key]
            if edit.get("name"):
                name = str(edit["name"]).strip()
                if len(name) > MAX_NAME_CHARS:
                    warnings.append(f"「{n.name}」名称改后超长，截断为 {MAX_NAME_CHARS} 字")
                    name = name[:MAX_NAME_CHARS]
                edited = True
            if edit.get("summary"):
                summary = str(edit["summary"]).strip()
                if len(summary) > MAX_SUMMARY_CHARS:
                    warnings.append(f"「{n.name}」概要改后超长，截断为 {MAX_SUMMARY_CHARS} 字")
                    summary = summary[:MAX_SUMMARY_CHARS]
                edited = True
            if not name or not summary:
                warnings.append(f"「{n.name}」编辑后为空，该节点被删除")
                continue

        cat_key = reassign.get(n.key, n.category_key)
        if cat_key not in cat_keys:
            warnings.append(f"「{n.name}」的目标分类 {cat_key} 不存在，归入第 0 类")
            cat_key = fallback_cat
        if cat_key is None:
            warnings.append(f"「{n.name}」无可用分类，该节点被删除")
            continue

        nodes.append(
            GraphDraftNode(
                key=n.key,
                name=name,
                weight=n.weight,
                summary=summary,
                quote=n.quote,
                loc_page=n.loc_page,
                category_key=cat_key,
                links=list(n.links),
                edited=edited or n.edited,
            )
        )

    new_cats = [c for c in draft.categories if c.key in {n.category_key for n in nodes}]
    for i, c in enumerate(new_cats):
        c.sort_order = i
    return (
        GraphDraft(
            categories=new_cats,
            nodes=nodes,
            edges=edges_from_nodes(nodes),
            unit_count=draft.unit_count,
            warnings=[*draft.warnings, *warnings],
        ),
        warnings,
    )


__all__ = [
    "GraphDraft",
    "GraphDraftCategory",
    "GraphDraftNode",
    "apply_edits",
    "edges_from_nodes",
    "merge_units",
]

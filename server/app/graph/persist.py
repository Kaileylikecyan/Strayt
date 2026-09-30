"""F11 ⑧ 入库：把确认后的图谱草稿落成 ``Category`` / ``Node`` / ``CardQuote`` /
``Edge`` 四张正式表。

三条纪律（与背诵管线的 ``persist.save_piece`` 同源）：

1. **确认即覆盖。** 预览确认是用户显式动作，语义与「显式 overwrite」一致：
   重新确认就替换项目里**旧的整份图谱**。和新提取对比的一致性由客户端负责
   提示（少了哪些旧节点）。
2. **一个事务。** 分类、节点、引文、关联要么全进、要么全不进，不能留半张图。
3. **入库不提交事务**，由调用方（API/引擎）决定；这里只做落行与计数。

长度守卫放两层：抽取器已按 ``MAX_*_CHARS`` 截断，这里再兜底一次 ——
LLM/人工编辑的输入都可能绕过头一层，直接在 ``INSERT`` 时炸 1406 不值得。
空的草稿（没有任何节点）直接拒收。
"""

from __future__ import annotations

from app.db.models import CardQuote, Category, Edge, Node, new_id
from app.graph.merge import GraphDraft
from app.persist import SaveOutcome, SaveResult
from sqlalchemy import delete, select
from sqlalchemy.orm import Session


def save_graph(
    db: Session,
    *,
    project_id: str,
    file_id: str | None,
    draft: GraphDraft,
) -> SaveResult:
    """把确认后的草稿落库。**不提交事务**，由调用方决定。

    返回的 ``reason`` 里有丢弃/裁切的说明，客户端可提示用户。
    """
    if not draft.nodes:
        return SaveResult(
            outcome=SaveOutcome.REJECTED,
            reason="草稿里没有任何知识点，无法入库",
            failure={"reason": "graph_empty", "retryable": False},
        )
    if not file_id:
        return SaveResult(
            outcome=SaveOutcome.REJECTED,
            reason="缺少文件出处，无法入库",
            failure={"reason": "graph_no_file", "retryable": False},
        )

    # 1) 清掉项目里旧的整份图谱（覆盖语义）
    _wipe_graph(db, project_id)

    cat_id_of: dict[str, str] = {}
    for c in draft.categories:
        cat = Category(
            id=new_id(),
            project_id=project_id,
            name=c.name,
            sort_order=c.sort_order,
        )
        db.add(cat)
        db.flush()
        cat_id_of[c.key] = cat.id

    node_id_of: dict[str, str] = {}
    for n in draft.nodes:
        node = Node(
            id=new_id(),
            project_id=project_id,
            category_id=cat_id_of.get(n.category_key),
            name=_clamp(n.name, 200),
            weight=_clamp_weight(n.weight),
            card_summary=_clamp(n.summary, 600),
            edited=n.edited,
        )
        db.add(node)
        db.flush()
        node_id_of[n.key] = node.id

    # 2) 引文：每节点一条（模型只给一条 quote）。``page`` 用篇起始页（P1 精度），
    #    ``para`` 记篇内序号，先保证出处可回看定位到篇，页内精确定位二期再细化。
    for n in draft.nodes:
        quote_text = _clamp(n.quote, 800)
        if not quote_text:
            continue
        db.add(
            CardQuote(
                id=new_id(),
                node_id=node_id_of[n.key],
                quote_text=quote_text,
                file_id=file_id,
                page=n.loc_page or None,
                para=None,
                sort_order=0,
            )
        )

    # 3) 边：双向去重、自环与死边在 ``edges_from_nodes`` 已处理，落库仍兜底。
    seen: set[tuple[str, str]] = set()
    edge_count = 0
    for from_key, to_key, reason in draft.edges:
        if from_key not in node_id_of or to_key not in node_id_of:
            continue
        pair = (from_key, to_key)
        if pair in seen or (to_key, from_key) in seen:
            continue
        seen.add(pair)
        db.add(
            Edge(
                id=new_id(),
                project_id=project_id,
                from_node=node_id_of[from_key],
                to_node=node_id_of[to_key],
                reason=_clamp(reason, 200) or None,
            )
        )
        edge_count += 1

    db.flush()
    return SaveResult(
        outcome=SaveOutcome.REPLACED,
        reason=(
            f"已入库 {len(draft.nodes)} 个知识点、{len(draft.categories)} 个分类、"
            f"{edge_count} 条关联"
        ),
    )


def _wipe_graph(db: Session, project_id: str) -> None:
    """清掉项目旧的整份图谱。按 FK 依赖反序删，避免外键约束冲突。"""
    db.execute(
        delete(CardQuote).where(
            CardQuote.node_id.in_(select(Node.id).where(Node.project_id == project_id))
        )
    )
    db.execute(delete(Edge).where(Edge.project_id == project_id))
    db.execute(delete(Node).where(Node.project_id == project_id))
    db.execute(delete(Category).where(Category.project_id == project_id))


def _clamp(s: str, limit: int) -> str:
    return s[:limit]


def _clamp_weight(w: int) -> int:
    return 2 if w not in (1, 3) else w


__all__ = ["save_graph"]

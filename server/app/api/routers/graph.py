"""图谱型项目的后端接口。

一条「加工 → 确认 → 展示」链的三种态：

1. ``POST /jobs`` (``graph_extract``)：抽取，结果只在 ``jobs.results_json`` 里，
   正式表还是空的 —— 这就是「预览态」；
2. ``POST /jobs/{id}/graph-confirm``：把预览草稿（可带删/改/调）一次性**原子**
   落进 ``categories`` / ``nodes`` / ``card_quotes`` / ``edges``；
3. ``GET /projects/{id}/graph``：确认后全量拉图（图谱视图用）。

同一文件还挂 F15 闪卡自测与 F16 掌握度统计：
闪卡由已确认节点确定性重建（question=名称，answer=概要/摘录，不调 LLM），
自测结果经 ``next_mastery`` 推进落回 ``nodes.mastery``。
"""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.routers.projects import load_project
from app.core.deps import get_db
from app.core.errors import ErrorCode, conflict, not_found
from app.db.models import (
    CardQuote,
    Category,
    CategoryRule,
    Edge,
    Flashcard,
    Job,
    Node,
    Project,
    new_id,
)
from app.graph.merge import GraphDraft, apply_edits
from app.graph.persist import save_graph
from app.graph.progress import next_mastery
from app.persist import SaveOutcome

router = APIRouter(prefix="/projects", tags=["知识图谱"])
router2 = APIRouter(prefix="/jobs", tags=["知识图谱"])
router3 = APIRouter(prefix="/nodes", tags=["知识图谱"])
router4 = APIRouter(prefix="/flashcards", tags=["闪卡自测"])


class NodeEdit(BaseModel):
    """预览确认时对一个节点的有限手动编辑（F14）。"""

    name: str | None = None
    summary: str | None = None


class GraphConfirmIn(BaseModel):
    """预览确认时的三种调整：删误抽 / 调分类 / 改内容。"""

    delete_keys: list[str] = Field(default_factory=list)
    reassign: dict[str, str] = Field(default_factory=dict)
    edits: dict[str, NodeEdit] = Field(default_factory=dict)


class GraphConfirmOut(BaseModel):
    ok: bool
    reason: str
    node_count: int
    category_count: int
    edge_count: int
    edited_nodes: int


class CategoryRuleIn(BaseModel):
    """一条分类规则（F12）。``match_on``=文件名/篇章标题，``kind``=前缀/包含/正则。"""

    match_on: Literal["file_name", "unit_title"]
    kind: Literal["prefix", "contains", "regex"]
    pattern: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=120)
    enabled: bool = True

    @field_validator("pattern", "category")
    @classmethod
    def _reject_blank(cls, v: str) -> str:
        """纯空白必须挡下，不能靠 ``min_length=1``。

        空 pattern 不是「无害地什么都不匹配」：``"".startswith("")``、
        ``"" in s``、``re.search("", s)`` 全都返回真，一条全空白的规则会把
        **整个项目**的节点倒进它指定的分类，且没有任何提示。入库时才发现就晚了。
        """
        s = v.strip()
        if not s:
            raise ValueError("不能为空或纯空白")
        return s

    @model_validator(mode="after")
    def _reject_bad_regex(self) -> CategoryRuleIn:
        """``kind="regex"`` 时当场编译一次。

        ``CategoryRule.match`` 对 ``re.error`` 是静默返回 False 的（避免一条坏规则
        打断整条管线），代价是写错正则的规则**永远不生效且毫无提示**。所以在入口
        就报错，让作者当场看见。
        """
        if self.kind == "regex":
            try:
                re.compile(self.pattern)
            except re.error as e:
                raise ValueError(f"正则表达式非法：{e}") from e
        return self


class CategoryRuleOut(BaseModel):
    id: str
    match_on: str
    kind: str
    pattern: str
    category: str
    enabled: bool
    #: 优先级，越小越先匹配。客户端**不需要**传这个字段 ——
    #: 它由 PUT 时的数组下标决定（见 CategoryRuleListIn）。
    priority: int


class CategoryRuleListIn(BaseModel):
    """整表覆盖保存：客户端把管理面板里的规则全量回传。

    **数组顺序即优先级**：第 0 条最先匹配。所以客户端拖拽排序后直接按序回传，
    服务端把它写进 ``priority`` 列。不要改成按 ``id`` 排序 —— ``id`` 是随机的。
    """

    rules: list[CategoryRuleIn] = Field(max_length=50)


@router.get(
    "/{project_id}/category-rules", response_model=list[CategoryRuleOut], summary="一级分类规则列表"
)
def list_category_rules(project_id: str, db: Session = Depends(get_db)) -> list[CategoryRuleOut]:
    load_project(db, project_id)
    rows = db.scalars(
        select(CategoryRule)
        .where(CategoryRule.project_id == project_id)
        .order_by(CategoryRule.priority, CategoryRule.id)
    )
    return [
        CategoryRuleOut(
            id=r.id,
            match_on=r.match_on,
            kind=r.kind,
            pattern=r.pattern,
            category=r.category,
            enabled=r.enabled,
            priority=r.priority,
        )
        for r in rows
    ]


@router.put(
    "/{project_id}/category-rules",
    response_model=list[CategoryRuleOut],
    summary="全量保存一级分类规则",
)
def put_category_rules(
    project_id: str, body: CategoryRuleListIn, db: Session = Depends(get_db)
) -> list[CategoryRuleOut]:
    """整表覆盖：先删该项目全部规则再按传入顺序重建，规则按 id 升序生效。"""
    load_project(db, project_id)
    db.execute(delete(CategoryRule).where(CategoryRule.project_id == project_id))
    saved: list[CategoryRuleOut] = []
    for priority, r in enumerate(body.rules):
        row = CategoryRule(
            id=new_id(),
            project_id=project_id,
            match_on=r.match_on,
            kind=r.kind,
            pattern=r.pattern.strip(),
            category=r.category.strip(),
            enabled=r.enabled,
            priority=priority,
        )
        db.add(row)
        saved.append(
            CategoryRuleOut(
                id=row.id,
                match_on=row.match_on,
                kind=row.kind,
                pattern=row.pattern,
                category=row.category,
                enabled=row.enabled,
                priority=row.priority,
            )
        )
    db.commit()
    return saved


class GraphDraftOut(BaseModel):
    job_id: str
    status: str
    draft: dict


class MasteryIn(BaseModel):
    mastery: Literal["no", "mid", "yes"]


class NodeMasteryOut(BaseModel):
    id: str
    mastery: str
    updated_at: str


class CategoryMasteryOut(BaseModel):
    category_id: str | None
    name: str
    total: int
    no: int
    mid: int
    yes: int


class MasteryStatsOut(BaseModel):
    project_id: str
    total: int
    by_mastery: dict[str, int]
    mastery_ratio: float
    by_category: list[CategoryMasteryOut]


class FlashcardGenOut(BaseModel):
    ok: bool
    created: int
    skipped: int


class FlashcardOut(BaseModel):
    id: str
    node_id: str
    question: str
    answer: str
    page: int | None
    category_id: str | None
    mastery: str


class FlashcardResultIn(BaseModel):
    correct: bool


class FlashcardResultOut(BaseModel):
    flashcard_id: str
    node_id: str
    node_mastery: str


# ==========================================================================
# 接口
# ==========================================================================
@router2.get("/{job_id}/graph-draft", response_model=GraphDraftOut, summary="阅读取预览草稿")
def get_graph_draft(job_id: str, db: Session = Depends(get_db)) -> dict:
    """抽取任务完成后的预览草稿（尚未落正式表）。"""
    job = db.get(Job, job_id)
    if job is None:
        raise not_found("任务", job_id)
    results = job.results_json or {}
    if "draft" not in results:
        raise conflict(ErrorCode.GRAPH_NO_DRAFT, "该任务还没有图谱草稿")
    return {
        "job_id": job.id,
        "status": results.get("status", "pending_confirm"),
        "draft": results["draft"],
    }


@router2.post("/{job_id}/graph-confirm", response_model=GraphConfirmOut, summary="预览确认并入库")
def confirm_graph(
    job_id: str, body: GraphConfirmIn, db: Session = Depends(get_db)
) -> GraphConfirmOut:
    """把草稿（带手动调整）一次性原子落库，覆盖项目旧图谱。"""
    job = db.get(Job, job_id)
    if job is None:
        raise not_found("任务", job_id)
    if job.type != "graph_extract":
        raise conflict(ErrorCode.VALIDATION, f"{job.type} 任务没有图谱草稿")
    if job.status != "success":
        raise conflict(ErrorCode.GRAPH_NO_DRAFT, f"任务状态 {job.status}，请先跑完抽取")
    results = job.results_json or {}
    if "draft" not in results:
        raise conflict(ErrorCode.GRAPH_NO_DRAFT, "该任务没有图谱草稿")
    draft = GraphDraft.from_dict(results["draft"])
    file_id = (job.checkpoint_json or {}).get("file_id")

    draft, warnings = apply_edits(
        draft,
        delete_keys=set(body.delete_keys),
        reassign=body.reassign,
        edits={k: v.model_dump(exclude_none=True) for k, v in body.edits.items()},
    )
    saved = save_graph(db, project_id=job.project_id, file_id=file_id, draft=draft)
    db.commit()

    if saved.outcome == SaveOutcome.REJECTED:
        raise conflict(ErrorCode.GRAPH_REJECTED, saved.reason or "入库被拒")
    # 确认成功就清掉草稿标记，避免重复确认保留两套状态
    job.results_json = {**results, "status": "confirmed"}
    db.commit()
    edited = sum(1 for n in draft.nodes if n.edited)
    return GraphConfirmOut(
        ok=True,
        reason=saved.reason or "已入库",
        node_count=len(draft.nodes),
        category_count=len({n.category_key for n in draft.nodes}),
        edge_count=len(draft.edges),
        edited_nodes=edited,
        warnings=warnings,
    )


@router.get("/{project_id}/graph", summary="图谱全量拉取")
def get_graph(project_id: str, db: Session = Depends(get_db)) -> dict:
    """确认入库后的完整图谱：分类 → 节点（带引文）→ 关联。"""
    project = db.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise not_found("项目", project_id)

    cats = list(
        db.scalars(
            select(Category)
            .where(Category.project_id == project_id)
            .order_by(Category.sort_order, Category.name)
        )
    )
    nodes = list(
        db.scalars(
            select(Node)
            .where(Node.project_id == project_id)
            .order_by(Node.category_id, Node.created_at)
        )
    )
    quotes = {
        q.node_id: q
        for q in db.scalars(select(CardQuote).where(CardQuote.node_id.in_([n.id for n in nodes])))
    }
    edges = list(db.scalars(select(Edge).where(Edge.project_id == project_id)))

    nodes_out = []
    for n in nodes:
        q = quotes.get(n.id)
        nodes_out.append(
            {
                "id": n.id,
                "category_id": n.category_id,
                "name": n.name,
                "weight": n.weight,
                "card_summary": n.card_summary,
                "user_notes": n.user_notes,
                "edited": n.edited,
                "mastery": n.mastery,
                "created_at": str(n.created_at),
                "updated_at": str(n.updated_at),
                "quote": {"text": q.quote_text if q else None, "page": q.page if q else None},
            }
        )
    return {
        "project_id": project_id,
        "categories": [{"id": c.id, "name": c.name, "sort_order": c.sort_order} for c in cats],
        "nodes": nodes_out,
        "edges": [
            {"from_node": e.from_node, "to_node": e.to_node, "reason": e.reason} for e in edges
        ],
    }


# ==========================================================================
# F16 掌握度统计与标记
# ==========================================================================
@router.get("/{project_id}/mastery-stats", response_model=MasteryStatsOut, summary="掌握度统计")
def get_mastery_stats(project_id: str, db: Session = Depends(get_db)) -> MasteryStatsOut:
    """按掌握度三级与一级分类做聚合，供图谱视图着色与进度概览。"""
    project = db.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise not_found("项目", project_id)

    cats = list(
        db.scalars(
            select(Category)
            .where(Category.project_id == project_id)
            .order_by(Category.sort_order, Category.name)
        )
    )
    nodes = list(db.scalars(select(Node).where(Node.project_id == project_id)))
    by_cat: dict[str | None, dict[str, int]] = {}
    for n in nodes:
        bucket = by_cat.setdefault(n.category_id, {"no": 0, "mid": 0, "yes": 0})
        bucket[n.mastery] += 1

    by_cat_out: list[CategoryMasteryOut] = []
    for c in cats:
        bucket = by_cat.pop(c.id, {"no": 0, "mid": 0, "yes": 0})
        by_cat_out.append(
            CategoryMasteryOut(
                category_id=c.id,
                name=c.name,
                total=sum(bucket.values()),
                **bucket,
            )
        )
    for cid, bucket in by_cat.items():
        by_cat_out.append(
            CategoryMasteryOut(
                category_id=cid,
                name="未分类",
                total=sum(bucket.values()),
                **bucket,
            )
        )

    total = len(nodes)
    by_mastery = {
        "no": sum(1 for n in nodes if n.mastery == "no"),
        "mid": sum(1 for n in nodes if n.mastery == "mid"),
        "yes": sum(1 for n in nodes if n.mastery == "yes"),
    }
    return MasteryStatsOut(
        project_id=project_id,
        total=total,
        by_mastery=by_mastery,
        mastery_ratio=round((by_mastery["mid"] + by_mastery["yes"]) / total, 4) if total else 0.0,
        by_category=by_cat_out,
    )


@router3.put("/{node_id}/mastery", response_model=NodeMasteryOut, summary="设置节点掌握度")
def put_node_mastery(
    node_id: str, body: MasteryIn, db: Session = Depends(get_db)
) -> NodeMasteryOut:
    """直接写掌握度（在线路径）。离线走弱同步 ``node_mastery`` 实体。"""
    node = db.get(Node, node_id)
    if node is None:
        raise not_found("节点", node_id)
    node.mastery = body.mastery
    db.commit()
    db.refresh(node)
    return NodeMasteryOut(id=node.id, mastery=node.mastery, updated_at=str(node.updated_at))


# ==========================================================================
# F15 闪卡自测
# ==========================================================================
@router.post(
    "/{project_id}/flashcards/generate",
    response_model=FlashcardGenOut,
    summary="按当前图谱重建闪卡（幂等）",
)
def generate_flashcards(project_id: str, db: Session = Depends(get_db)) -> FlashcardGenOut:
    """从已确认节点确定性重建整副卡组：question=名称，answer=概要/摘录。

    不调 LLM —— 概要/摘录在抽取时已限"仅基于给定材料"，这里复用它。
    同一事务先清旧卡再全量插入（与图谱擦除重建同语义）。
    """
    project = db.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise not_found("项目", project_id)

    nodes = list(
        db.scalars(select(Node).where(Node.project_id == project_id).order_by(Node.created_at))
    )
    quotes = {
        q.node_id: q
        for q in db.scalars(select(CardQuote).where(CardQuote.node_id.in_([n.id for n in nodes])))
    }

    cards: list[Flashcard] = []
    skipped = 0
    for n in nodes:
        answer = n.card_summary or (quotes[n.id].quote_text if n.id in quotes else "")
        if not answer:
            skipped += 1
            continue
        cards.append(Flashcard(node_id=n.id, question=n.name, answer=answer))

    db.execute(
        delete(Flashcard).where(
            Flashcard.node_id.in_(select(Node.id).where(Node.project_id == project_id))
        )
    )
    db.add_all(cards)
    db.commit()
    return FlashcardGenOut(ok=True, created=len(cards), skipped=skipped)


@router.get("/{project_id}/flashcards", response_model=list[FlashcardOut], summary="闪卡卡组")
def list_flashcards(
    project_id: str,
    mastery: Literal["no", "mid", "yes"] | None = None,
    db: Session = Depends(get_db),
) -> list[FlashcardOut]:
    """按项目取卡组，可只取某掌握度（自测优先复习弱项）。"""
    project = db.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise not_found("项目", project_id)

    q = (
        select(Flashcard, Node)
        .join(Node, Flashcard.node_id == Node.id)
        .where(Node.project_id == project_id)
        .order_by(Node.category_id, Node.created_at)
    )
    if mastery is not None:
        q = q.where(Node.mastery == mastery)

    cards = db.execute(q).all()
    quotes = {
        qq.node_id: qq
        for qq in db.scalars(
            select(CardQuote).where(CardQuote.node_id.in_([fc.node_id for fc, _ in cards]))
        )
    }
    return [
        FlashcardOut(
            id=fc.id,
            node_id=fc.node_id,
            question=fc.question,
            answer=fc.answer,
            page=(quotes[fc.node_id].page if fc.node_id in quotes else None),
            category_id=n.category_id,
            mastery=n.mastery,
        )
        for fc, n in cards
    ]


@router4.post("/{flashcard_id}/result", response_model=FlashcardResultOut, summary="自测结果回写")
def post_flashcard_result(
    flashcard_id: str, body: FlashcardResultIn, db: Session = Depends(get_db)
) -> FlashcardResultOut:
    """答对/答错推进一档掌握度（no↔mid↔yes），两端钳位。"""
    fc = db.get(Flashcard, flashcard_id)
    if fc is None:
        raise not_found("闪卡", flashcard_id)
    node = db.get(Node, fc.node_id)
    if node is None:
        raise not_found("节点", fc.node_id)
    node.mastery = next_mastery(node.mastery, correct=body.correct)
    db.commit()
    db.refresh(node)
    return FlashcardResultOut(flashcard_id=fc.id, node_id=node.id, node_mastery=node.mastery)

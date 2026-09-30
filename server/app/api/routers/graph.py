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

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.core.errors import ErrorCode, conflict, not_found
from app.db.models import CardQuote, Category, Edge, Flashcard, Job, Node, Project
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
    warnings: list[str] = Field(default_factory=list)


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

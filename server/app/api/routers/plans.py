"""学习路径闭环读取侧（文档 F22）。

``goals`` / ``plans`` 的写入走弱同步（sync 的 ``goal`` / ``plan`` 实体，
离线也能排队重放），这里只做读取聚合，不提供改写的第二个入口：

- ``GET /plans``       阶段计划列表。排序：未完成优先 → 逾期最先 → 到期日升序
                       → 无到期日按 ``sort_order``；已完成一律排最后。
                       每条附 ``days_until`` / ``overdue``（相对排序参考日）。
- ``GET /plans/daily`` 每日待办聚合。把未完成计划按到期日相对参考日分四桶：
                       逾期 / 今天 / 之后 / 未排期；已完成的单列（完成记录）。

「逾期顺延」的产品语义暂不做（模型注释：一期只做简单待办），职责是让客户端
一眼看到"今天该做什么、欠了多少"。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import PlanListOut
from app.core.deps import get_db
from app.db.models import Plan

router = APIRouter(prefix="/plans", tags=["学习路径"])

_BUCKET = {"overdue": 0, "today": 1, "later": 2, "unscheduled": 3}


def _today() -> date:
    return date.today()


def _annotate(p: Plan, ref: date) -> Plan:
    """给 ORM 行挂排序/展示用派生字段（与 recite 的 ``pair_count`` 同套手法）。"""
    p.days_until = (p.due_date - ref).days if p.due_date else None
    p.overdue = p.due_date is not None and p.due_date < ref
    return p


def _to_display(p: Plan) -> dict:
    """每日待办聚合项 → 字典（响应无 schema，避免 pydantic 序列化 ORM 对象）。"""
    return {
        "id": p.id,
        "project_id": p.project_id,
        "title": p.title,
        "due_date": p.due_date,
        "status": p.status,
        "target_type": p.target_type,
        "target_id": p.target_id,
        "sort_order": p.sort_order,
        "days_until": p.days_until,
        "overdue": p.overdue,
    }


def _bucket(p: Plan, ref: date) -> int:
    if p.status == "done":
        return 99
    if p.due_date is None:
        return _BUCKET["unscheduled"]
    if p.due_date < ref:
        return _BUCKET["overdue"]
    if p.due_date == ref:
        return _BUCKET["today"]
    return _BUCKET["later"]


def _sort_key(p: Plan, ref: date):
    return (
        p.status == "done",
        _bucket(p, ref),
        p.due_date or date.max,
        p.sort_order,
    )


@router.get("", response_model=list[PlanListOut], summary="阶段计划列表")
def list_plans(
    project_id: str | None = None,
    status: str | None = Query(None, pattern="^(todo|doing|done)$"),
    ref_date: date | None = Query(None, description="排序参考日（used for 逾期判定），默认今天"),
    db: Session = Depends(get_db),
) -> list[Plan]:
    stmt = select(Plan)
    if project_id:
        stmt = stmt.where(Plan.project_id == project_id)
    if status:
        stmt = stmt.where(Plan.status == status)
    rows = [_annotate(p, ref_date or _today()) for p in db.scalars(stmt)]
    rows.sort(key=lambda p: _sort_key(p, ref_date or _today()))
    return rows


@router.get("/daily", summary="每日待办聚合")
def daily_todos(
    day: date | None = Query(None, description="待办日，默认今天"),
    db: Session = Depends(get_db),
) -> dict:
    ref = day or _today()
    buckets = {
        "overdue": [],
        "today": [],
        "later": [],
        "unscheduled": [],
        "done": [],
    }

    def _key(p: Plan, ref: date) -> str:
        if p.status == "done":
            return "done"
        if p.due_date is None:
            return "unscheduled"
        if p.due_date < ref:
            return "overdue"
        if p.due_date == ref:
            return "today"
        return "later"

    for p in db.scalars(select(Plan)):
        _annotate(p, ref)
        buckets[_key(p, ref)].append(p)

    for name, items in buckets.items():
        if name in ("unscheduled", "today"):
            items.sort(key=lambda p: p.sort_order)
        else:
            items.sort(key=lambda p: ((p.due_date or date.min), p.sort_order))

    open_count = sum(len(buckets[k]) for k in ("overdue", "today", "later", "unscheduled"))
    return {
        "date": ref,
        "stats": {
            "total": len(buckets["done"]) + open_count,
            "open": open_count,
            "done": len(buckets["done"]),
        },
        "overdue": [_to_display(x) for x in buckets["overdue"]],
        "today": [_to_display(x) for x in buckets["today"]],
        "later": [_to_display(x) for x in buckets["later"]],
        "unscheduled": [_to_display(x) for x in buckets["unscheduled"]],
        "done": [_to_display(x) for x in buckets["done"]],
    }

"""项目 CRUD（文档 F4）。

规则：
- ``type`` 创建后**不可改**。改类型等于换一种加工管线，历史数据无法复用，
  所以这里刻意不提供 update 入口，改名可以，改类型不行。
- 删除是软删（``deleted_at``）进回收站，保留 N 天；文件同步移入回收站目录。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import GoalIn, GoalOut, ProjectCreate, ProjectOut
from app.core.deps import get_db
from app.core.errors import ErrorCode, conflict, not_found
from app.db.models import Goal, Job, Project, new_id, utcnow

router = APIRouter(prefix="/projects", tags=["项目"])


def load_project(db: Session, project_id: str, *, allow_deleted: bool = False) -> Project:
    p = db.get(Project, project_id)
    if p is None or (p.deleted_at is not None and not allow_deleted):
        raise not_found("项目", project_id)
    return p


@router.get("", response_model=list[ProjectOut], summary="项目列表")
def list_projects(
    db: Session = Depends(get_db),
    include_deleted: bool = Query(False, description="回收站视图：只列已删项目"),
    type: str | None = Query(None, pattern="^(graph|recite)$"),
) -> list[Project]:
    stmt = select(Project)
    if include_deleted:
        stmt = stmt.where(Project.deleted_at.is_not(None))
    else:
        stmt = stmt.where(Project.deleted_at.is_(None))
    if type:
        stmt = stmt.where(Project.type == type)
    return list(db.scalars(stmt.order_by(Project.created_at.desc())))


@router.post("", response_model=ProjectOut, status_code=201, summary="新建项目")
def create_project(body: ProjectCreate, db: Session = Depends(get_db)) -> Project:
    p = Project(name=body.name.strip(), type=body.type)
    if body.api_profile_id:
        p.api_profile_json = {"model_profile_id": body.api_profile_id}
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@router.get("/{project_id}", response_model=ProjectOut, summary="项目详情")
def get_project(project_id: str, db: Session = Depends(get_db)) -> Project:
    return load_project(db, project_id)


@router.patch("/{project_id}", response_model=ProjectOut, summary="改名（类型不可改）")
def rename_project(project_id: str, name: str, db: Session = Depends(get_db)) -> Project:
    p = load_project(db, project_id)
    p.name = name.strip()
    db.commit()
    db.refresh(p)
    return p


@router.delete("/{project_id}", status_code=204, summary="软删进回收站")
def delete_project(project_id: str, db: Session = Depends(get_db)) -> None:
    p = load_project(db, project_id)
    active = db.scalar(
        select(Job.id).where(Job.project_id == project_id, Job.status.in_(("queued", "running")))
    )
    if active:
        raise conflict(ErrorCode.JOB_NOT_RUNNABLE, "该项目有正在跑的加工任务，请先取消再删除")

    p.deleted_at = utcnow()
    db.commit()


@router.post("/{project_id}/restore", response_model=ProjectOut, summary="从回收站恢复")
def restore_project(project_id: str, db: Session = Depends(get_db)) -> Project:
    p = load_project(db, project_id, allow_deleted=True)
    p.deleted_at = None
    db.commit()
    db.refresh(p)
    return p


# --------------------------------------------------------------------------
# 考试目标（F22 一期最小集）
# --------------------------------------------------------------------------
@router.get("/{project_id}/goal", response_model=GoalOut, summary="考试目标")
def get_goal(project_id: str, db: Session = Depends(get_db)) -> Goal:
    load_project(db, project_id)
    goal = db.scalar(select(Goal).where(Goal.project_id == project_id))
    if goal is None:
        goal = Goal(id=new_id(), project_id=project_id, exam_date=utcnow().date())
        db.add(goal)
        db.commit()
        db.refresh(goal)
    return goal


@router.put("/{project_id}/goal", response_model=GoalOut, summary="设置考试目标")
def put_goal(project_id: str, body: GoalIn, db: Session = Depends(get_db)) -> Goal:
    load_project(db, project_id)
    goal = db.scalar(select(Goal).where(Goal.project_id == project_id))
    if goal is None:
        goal = Goal(id=new_id(), project_id=project_id, exam_date=body.exam_date)
        db.add(goal)
    else:
        goal.exam_date = body.exam_date
    db.commit()
    db.refresh(goal)
    return goal

"""加工任务（文档 §5）。

``recite_align`` 背诵项目的中英对齐入库；``graph_extract`` 图谱项目的
LLM 知识点抽取（结果存草稿，预览确认后才入库，见 ``app/api/graph.py``）。
任务在**服务端后台线程**跑，客户端可关窗；状态落 ``jobs`` 表，
``checkpoint_json`` 支持断点续跑。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import JobCreate, JobOut
from app.core.deps import get_db
from app.core.errors import ErrorCode, conflict, not_found
from app.db.models import File, Job, Project, utcnow
from app.jobs.engine import run_manager
from app.jobs.graph import NoProviderError, resolve_extractor

router = APIRouter(prefix="/jobs", tags=["加工任务"])

#: 项目类型 → 允许的任务类型
JOB_TYPES = {"recite_align", "graph_extract"}
_PROJECT_JOB_TYPES = {"recite": "recite_align", "graph": "graph_extract"}


@router.post("", response_model=JobOut, status_code=201, summary="新建加工任务并开跑")
def create_job(body: JobCreate, db: Session = Depends(get_db)) -> Job:
    """入队并立即在服务端后台开跑，客户端可以关窗。

    任务表里同时写 ``file_id``，断点续跑靠它找回要加工的文件。
    图谱任务在创建时就校验 LLM 配置：没有模型就没有知识点，与其建一个
    必然会失败的活，不如一开始就拦住并说明原因。
    """
    if body.type not in JOB_TYPES:
        raise conflict(ErrorCode.VALIDATION, f"暂不支持的任务类型 {body.type}")
    project = db.get(Project, body.project_id)
    if project is None or project.deleted_at is not None:
        raise not_found("项目", body.project_id)
    want = _PROJECT_JOB_TYPES.get(project.type)
    if want != body.type:
        raise conflict(
            ErrorCode.VALIDATION,
            f"{project.type} 型项目只能用 {want} 加工，不能用 {body.type}",
        )
    file_row = db.get(File, body.file_id)
    if file_row is None or file_row.deleted_at is not None:
        raise not_found("文件", body.file_id)
    if file_row.project_id != body.project_id:
        raise conflict(ErrorCode.VALIDATION, "文件不属于该项目")
    if body.type == "graph_extract":
        try:
            resolve_extractor(db, body.project_id)
        except NoProviderError as exc:
            raise conflict(ErrorCode.VALIDATION, str(exc)) from exc

    job = Job(
        project_id=body.project_id,
        type=body.type,
        status="queued",
        checkpoint_json={"file_id": body.file_id, "done": [], "failed": []},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    run_manager.submit(job.id, overwrite=body.overwrite)
    return job


@router.get("", response_model=list[JobOut], summary="任务列表")
def list_jobs(
    project_id: str | None = Query(None),
    db: Session = Depends(get_db),
    status: str | None = Query(
        None, pattern="^(queued|running|success|failed|cancelled|interrupted)$"
    ),
    limit: int = Query(50, ge=1, le=200),
) -> list[Job]:
    stmt = select(Job)
    if project_id:
        stmt = stmt.where(Job.project_id == project_id)
    if status:
        stmt = stmt.where(Job.status == status)
    return list(db.scalars(stmt.order_by(Job.created_at.desc()).limit(limit)))


@router.get("/{job_id}", response_model=JobOut, summary="任务详情（含逐章进度与断点）")
def get_job(job_id: str, db: Session = Depends(get_db)) -> Job:
    j = db.get(Job, job_id)
    if j is None:
        raise not_found("任务", job_id)
    return j


@router.post("/{job_id}/cancel", response_model=JobOut, summary="取消排队中、运行中或已中断的任务")
def cancel_job(job_id: str, db: Session = Depends(get_db)) -> Job:
    j = db.get(Job, job_id)
    if j is None:
        raise not_found("任务", job_id)
    if j.status not in ("queued", "running", "interrupted"):
        raise conflict(ErrorCode.JOB_NOT_RUNNABLE, f"任务已 {j.status}，无法取消")
    j.status = "cancelled"
    j.heartbeat_at = utcnow()
    db.commit()
    db.refresh(j)
    # 正在跑的那个单元不会被硬打断 —— 强杀协程会在库里留半个事务，代价远大于
    # 多等几十秒。引擎的心跳协程下一次轮询看到这个状态就会在单元边界停下。
    run_manager.request_cancel(job_id)
    return j


@router.post("/{job_id}/resume", response_model=JobOut, summary="断点续跑失败或已中断的任务")
def resume_job(job_id: str, db: Session = Depends(get_db)) -> Job:
    j = db.get(Job, job_id)
    if j is None:
        raise not_found("任务", job_id)
    if j.status not in ("failed", "cancelled", "interrupted"):
        raise conflict(ErrorCode.JOB_NOT_RUNNABLE, f"任务状态 {j.status}，无需续跑")
    # ``interrupted`` 是 F8 的超支熔断：用户点续跑即视为「确认继续」，
    # 引擎会在本轮关闭熔断（checkpoint 里的 ``budget_stop``），把任务跑完。
    j.status = "queued"
    j.error = None
    j.heartbeat_at = utcnow()
    db.commit()
    db.refresh(j)
    # 断点续跑：引擎按 ``checkpoint_json.done`` 跳过已入库的篇目
    run_manager.resume(job_id)
    return j

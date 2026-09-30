"""弱同步（架构红线 6 / 文档 F2）。

协议只有两个方向，刻意做窄，避免和服务端语义双向纠缠：

**下行 bootstrap**（打开应用时一次全量）
    ``GET /sync/bootstrap`` → 全量 snapshot + 服务端时钟。
    客户端覆盖本地 SQLite snapshot。``schema_version`` 与本地不一致时，
    客户端应清库重建而不是硬合并。

**上行 batch**（联网后按 client_ts 顺序重放写队列）
    ``POST /sync/batch`` → 每条 op 带 ``op_id`` 与 ``client_ts``。
    逐条返回 applied / skipped / conflict，**整批不因单条失败回滚**
    （离线队列里常有已被别人删掉的实体）。

``op_id`` 主要给客户端去重用。服务端天然幂等：一条 op 生效后 ``updated_at``
被置为服务端时间，重复重放时若 ``client_ts < updated_at``，会被下面的 LWW 判定
挡成 conflict，所以不需要额外存幂等表。

冲突裁决：以 ``updated_at`` 时间戳为准，晚的赢。这是 LWW(last-writer-wins)，
单机自用不做向量时钟 —— 复杂度换不来收益。

**已知缺口 —— 时钟偏移**：LWW 直接比较客户端的 ``client_ts`` 与服务端
``updated_at``。两台设备时钟不一致时，落后设备的离线写入可能被反复判为
「更新」而覆盖领先设备。单机自用下可接受（偏差通常分钟级），但要严格化的
正解是改用服务端收单时记录的单调 ``received_seq`` 做裁决，代价是客户端
重放时要接受服务端回传的重排序。这条留到二期，记一笔在
``tests/test_sync.py::test_replay_has_no_double_effect`` 的断言里。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import get_db
from app.core.errors import ErrorCode, bad_request
from app.db.models import (
    Checkin,
    File,
    Goal,
    Node,
    Pair,
    Piece,
    Plan,
    Project,
    utcnow,
)

router = APIRouter(prefix="/sync", tags=["弱同步"])

# 允许客户端写入的实体 → (模型, 允许改的字段)。白名单而非黑名单：
# 加新表时不会意外开放写权限。
_WRITABLE: dict[str, tuple[type, tuple[str, ...]]] = {
    "piece_progress": (Piece, ("last_pos", "recited")),
    "pair_edit": (Pair, ("zh", "en", "seq", "needs_review", "manually_edited")),
    "checkin": (Checkin, ("items_json",)),
    "plan": (Plan, ("title", "due_date", "status", "sort_order")),
    "goal": (Goal, ("exam_date",)),
    "node_mastery": (Node, ("mastery",)),
}


class OpIn(BaseModel):
    """一条写队列操作。"""

    op_id: str = Field(min_length=1, max_length=64, description="幂等键，客户端生成")
    entity: str = Field(description=f"可写实体：{', '.join(_WRITABLE)}")
    entity_id: str
    client_ts: datetime = Field(description="客户端写入时刻（UTC naive）")
    patch: dict[str, Any] = Field(default_factory=dict)

    @field_validator("client_ts")
    @classmethod
    def _to_naive_utc(cls, v: datetime) -> datetime:
        """带时区的 ``client_ts`` 一律折成 naive UTC。

        浏览器 ``Date#toISOString()`` 出来的是 ``2026-09-29T06:00:00.000Z``
        （aware），而 MySQL 里的 ``updated_at`` 是 naive。直接比较会
        ``TypeError: can't compare offset-naive and offset-aware datetimes``
        → 整批 500 —— 也就是说**任何真实客户端的第一次写操作都会炸**。
        """
        if v.tzinfo is not None:
            return v.astimezone(UTC).replace(tzinfo=None)
        return v


class BatchIn(BaseModel):
    ops: list[OpIn] = Field(max_length=200)


class OpResult(BaseModel):
    op_id: str
    status: Literal["applied", "skipped", "conflict", "error"]
    reason: str | None = None
    server_updated_at: datetime | None = None


class BatchOut(BaseModel):
    results: list[OpResult]
    server_time: datetime


@router.get("/bootstrap", summary="全量快照（打开应用时一次）")
def bootstrap(db: Session = Depends(get_db)) -> dict[str, Any]:
    s = get_settings()
    projects = list(db.scalars(select(Project).where(Project.deleted_at.is_(None))))

    return {
        "schema_version": s.snapshot_schema_version,
        "server_time": utcnow(),
        "projects": [
            {
                "id": p.id,
                "name": p.name,
                "type": p.type,
                "api_profile_json": p.api_profile_json,
                "created_at": p.created_at,
                "updated_at": p.updated_at,
            }
            for p in projects
        ],
        "files": [
            {
                "id": f.id,
                "project_id": f.project_id,
                "orig_name": f.orig_name,
                "sha256": f.sha256,
                "size": f.size,
                "parse_channel": f.parse_channel,
                "page_count": f.page_count,
                "uploaded_at": f.uploaded_at,
            }
            for f in db.scalars(select(File).where(File.deleted_at.is_(None)))
        ],
        "pieces": [
            {
                "id": x.id,
                "project_id": x.project_id,
                "title": x.title,
                "align_mode": x.align_mode,
                "recited": x.recited,
                "last_pos": x.last_pos,
                "sort_order": x.sort_order,
                "updated_at": x.updated_at,
            }
            for x in db.scalars(select(Piece))
        ],
        "pairs": [
            {
                "pair_key": p.pair_key,
                "piece_id": p.piece_id,
                "seq": p.seq,
                "zh": p.zh,
                "en": p.en,
                "loc_page": p.loc_page,
                "needs_review": p.needs_review,
                "manually_edited": p.manually_edited,
                "updated_at": p.updated_at,
            }
            for p in db.scalars(select(Pair).order_by(Pair.piece_id, Pair.seq))
        ],
        "goals": [
            {
                "id": g.id,
                "project_id": g.project_id,
                "exam_date": g.exam_date,
                "updated_at": g.updated_at,
            }
            for g in db.scalars(select(Goal))
        ],
        "plans": [
            {
                "id": pl.id,
                "project_id": pl.project_id,
                "title": pl.title,
                "due_date": pl.due_date,
                "status": pl.status,
                "target_type": pl.target_type,
                "target_id": pl.target_id,
                "sort_order": pl.sort_order,
                "updated_at": pl.updated_at,
            }
            for pl in db.scalars(select(Plan))
        ],
        "checkins": [
            {"date": c.date, "items_json": c.items_json, "updated_at": c.updated_at}
            for c in db.scalars(select(Checkin))
        ],
    }


@router.post("/batch", response_model=BatchOut, summary="重放写队列")
def push_batch(body: BatchIn, db: Session = Depends(get_db)) -> BatchOut:
    limit = get_settings().sync_batch_limit
    if len(body.ops) > limit:
        # 客户端自己该按 sync_batch_limit 分页；超限说明两端配置漂移，明确报错而非静默截断
        raise bad_request(
            ErrorCode.SYNC_STALE,
            f"单批上限 {limit} 条，超出请客户端分页重放",
            detail={"limit": limit, "got": len(body.ops)},
        )

    # 按 client_ts 升序重放，保证「先写的先落」
    ops = sorted(body.ops, key=lambda o: o.client_ts)
    results: list[OpResult] = []

    for op in ops:
        results.append(_apply_one(op, db))
        db.commit()  # 逐条提交：单条失败不拖垮整批

    return BatchOut(results=results, server_time=utcnow())


def _apply_one(op: OpIn, db: Session) -> OpResult:
    spec = _WRITABLE.get(op.entity)
    if spec is None:
        return OpResult(op_id=op.op_id, status="error", reason=f"不可写实体：{op.entity}")

    model, allowed = spec
    bad = set(op.patch) - set(allowed)
    if bad:
        return OpResult(op_id=op.op_id, status="error", reason=f"字段不可写：{sorted(bad)}")

    row = db.get(model, op.entity_id)
    if row is None and model is Pair:
        # 客户端的段落对没有暴露自增主键，以稳定 key ``pair_key`` 寻址（F18 手动编辑）
        row = db.scalar(select(Pair).where(Pair.pair_key == op.entity_id))
    if row is None:
        return OpResult(op_id=op.op_id, status="skipped", reason="实体已不存在（可能已被删除）")

    # LWW 裁决：客户端这次写入比服务端现有版本旧 → 丢弃
    if op.client_ts < row.updated_at:
        return OpResult(
            op_id=op.op_id,
            status="conflict",
            reason="服务端版本更新，丢弃本次写入",
            server_updated_at=row.updated_at,
        )

    for k, v in op.patch.items():
        setattr(row, k, v)
    row.updated_at = utcnow()
    db.flush()
    return OpResult(op_id=op.op_id, status="applied", server_updated_at=row.updated_at)

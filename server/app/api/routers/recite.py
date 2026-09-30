"""背诵舱（文档 F3）。

一期只有「交错背诵舱」一种形态：按 ``(zh, en)`` 段落对逐条推进。
四档强度是**客户端渲染策略**（每次显示中/英的比例 + 是否打乱），服务端只存
``last_pos`` / ``recited``，不在服务端做状态机。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.routers.projects import load_project
from app.api.schemas import CheckinIn, CheckinOut, PairOut, PieceOut, PieceProgressIn
from app.core.deps import get_db
from app.core.errors import not_found
from app.db.models import Checkin, Pair, Piece, utcnow

router = APIRouter(tags=["背诵舱 / 打卡"])


# ==========================================================================
# 篇目与段落对
# ==========================================================================
@router.get("/projects/{project_id}/pieces", response_model=list[PieceOut], summary="篇目列表")
def list_pieces(project_id: str, db: Session = Depends(get_db)) -> list[Piece]:
    load_project(db, project_id)
    counts = dict(
        db.execute(
            select(Pair.piece_id, func.count(Pair.id))
            .join(Piece, Piece.id == Pair.piece_id)
            .where(Piece.project_id == project_id)
            .group_by(Pair.piece_id)
        ).all()
    )
    out = list(
        db.scalars(select(Piece).where(Piece.project_id == project_id).order_by(Piece.sort_order))
    )
    for p in out:
        p.pair_count = counts.get(p.id, 0)
    return out


@router.get(
    "/projects/{project_id}/pieces/{piece_id}/pairs",
    response_model=list[PairOut],
    summary="取段落对（背诵舱数据源）",
)
def list_pairs(
    project_id: str,
    piece_id: str,
    db: Session = Depends(get_db),
    from_seq: int = Query(0, ge=0, description="续传起点"),
    limit: int = Query(500, ge=1, le=2000),
) -> list[Pair]:
    load_project(db, project_id)
    piece = db.get(Piece, piece_id)
    if piece is None or piece.project_id != project_id:
        raise not_found("篇目", piece_id)
    return list(
        db.scalars(
            select(Pair)
            .where(Pair.piece_id == piece_id, Pair.seq >= from_seq)
            .order_by(Pair.seq)
            .limit(limit)
        )
    )


@router.put(
    "/projects/{project_id}/pieces/{piece_id}/progress",
    response_model=PieceOut,
    summary="上报背诵进度",
)
def put_progress(
    project_id: str, piece_id: str, body: PieceProgressIn, db: Session = Depends(get_db)
) -> Piece:
    load_project(db, project_id)
    piece = db.get(Piece, piece_id)
    if piece is None or piece.project_id != project_id:
        raise not_found("篇目", piece_id)

    # 冲突裁决：客户端离线写带回的 client_ts 早于服务端已存进度 → 丢弃，不回退进度
    if body.client_ts < piece.updated_at:
        return piece

    piece.last_pos = body.last_pos
    if body.recited is not None:
        piece.recited = body.recited
    db.commit()
    db.refresh(piece)
    piece.pair_count = db.scalar(select(func.count(Pair.id)).where(Pair.piece_id == piece_id)) or 0
    return piece


# ==========================================================================
# 每日打卡
# ==========================================================================
@router.get("/checkins/{day}", response_model=CheckinOut, summary="查某天打卡")
def get_checkin(day: date, db: Session = Depends(get_db)) -> Checkin:
    c = db.get(Checkin, day)
    if c is None:
        c = Checkin(date=day, items_json=[])
        db.add(c)
        db.commit()
        db.refresh(c)
    return c


@router.put("/checkins", response_model=CheckinOut, summary="打卡（同日覆盖）")
def put_checkin(body: CheckinIn, db: Session = Depends(get_db)) -> Checkin:
    c = db.get(Checkin, body.date)
    if c is None:
        c = Checkin(date=body.date, items_json=body.items)
        db.add(c)
    else:
        c.items_json = body.items
    c.updated_at = utcnow()
    db.commit()
    db.refresh(c)
    return c

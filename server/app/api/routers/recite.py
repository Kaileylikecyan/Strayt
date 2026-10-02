"""背诵舱（文档 F3）。

一期只有「交错背诵舱」一种形态：按 ``(zh, en)`` 段落对逐条推进。
三档强度是**客户端渲染策略**（每次显示中/英的比例 + 是否打乱），服务端只存
``last_pos`` / ``recited``，不在服务端做状态机。

**背诵单元粒度（按句 / 按段）是客户端的事**（ADR-0012）：服务端存的 ``pairs`` 永远是
句级原子，段级分组由客户端按 ``block_no`` 在展示时合成。因此本模块的读接口只管吐
有序的句级对句，不关心用户当前选的粒度。

**进度与粒度解耦**：``last_pos`` 永远记「已背到第几句」。「某段背完」由客户端推导
（该段内所有 ``seq`` 都越过 ``last_pos``），所以用户切粒度不会毁掉进度。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.routers.projects import load_project
from app.api.schemas import (
    CheckinIn,
    CheckinOut,
    PairMergeIn,
    PairOut,
    PairSplitIn,
    PieceOut,
    PieceProgressIn,
)
from app.core.deps import get_db
from app.core.errors import ErrorCode, bad_request, not_found
from app.db.models import Checkin, Pair, Piece, new_id, utcnow

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


def _load_pair(db: Session, project_id: str, piece_id: str, pair_key: str) -> Pair:
    """按稳定 ``pair_key`` 取段落对，并校验属于指定篇目/项目。"""
    load_project(db, project_id)
    pair = db.scalar(select(Pair).where(Pair.pair_key == pair_key))
    if pair is None or pair.piece_id != piece_id:
        raise not_found("段落对", pair_key)
    return pair


def _renumber_pairs(db: Session, piece_id: str) -> list[Pair]:
    """把某篇所有段落对按当前 ``seq`` 顺序重新编号为 0..n-1，并返回并好的列表。

    拆分/合并都会制造 seq 空洞或重复，这一步保证 seq 总是稠密连续的，
    客户端背诵舱按 ``seq`` 渲染才不会错位。

    开头那个 ``flush`` 不能省：``SessionLocal`` 是 ``autoflush=False``（见
    ``app/db/session.py``），拆分/合并刚 ``add`` 的新对句还挂在 session 里，
    不先落库这条 SELECT 就查不到它们 —— 结果是新对句带着拆分前的旧 seq 混进来，
    和重编号过的旧行撞 seq，客户端排序直接错位。
    """
    db.flush()
    rows = list(db.scalars(select(Pair).where(Pair.piece_id == piece_id).order_by(Pair.seq)))
    for i, p in enumerate(rows):
        p.seq = i
        p.updated_at = utcnow()
    db.commit()
    for p in rows:
        db.refresh(p)
    return rows


@router.post(
    "/projects/{project_id}/pieces/{piece_id}/pairs/split",
    response_model=list[PairOut],
    summary="拆分段落对（拆成两个）",
)
def split_pair(
    project_id: str, piece_id: str, body: PairSplitIn, db: Session = Depends(get_db)
) -> list[Pair]:
    """把一个 ``(zh, en)`` 段落对按客户端算好的两半内容拆成两个新对。

    F18 结构化编辑：两句/两段并列在一个对里时手动拆开。拆的结果是**两个新的
    ``pair_key``**（稳定 key 算法与 AGENTS §7 的「人工内容与 AI 内容分区」一致），
    原对删除。两个新对都标 ``manually_edited``，覆盖重跑时不会被冲掉。

    两个新对**沿用原对的 ``block_no``**：拆开的两半本就在同一个原文段里，
    拆完仍属同一段。若拆完后需要按句背，切「按句」粒度即可，不靠块号区分。
    """
    pair = _load_pair(db, project_id, piece_id, body.pair_key)
    if not (body.zh_a.strip() and body.zh_b.strip() and body.en_a.strip() and body.en_b.strip()):
        raise bad_request(ErrorCode.VALIDATION, "拆分后的两半中英文都不能为空")
    # 先把尾部队列整体后移一位，避免与新对产生重复 seq（ix_pairs_piece_seq 非唯一，
    # seq 相同会导致重排顺序不稳定）
    tail = list(
        db.scalars(
            select(Pair)
            .where(Pair.piece_id == piece_id, Pair.seq > pair.seq)
            .order_by(Pair.seq.desc())
        )
    )
    for p in tail:
        p.seq += 1
    db.delete(pair)
    db.flush()
    sa = Pair(
        pair_key=new_id(),
        piece_id=piece_id,
        seq=pair.seq,
        zh=body.zh_a,
        en=body.en_a,
        loc_page=pair.loc_page,
        needs_review=pair.needs_review,
        confidence=pair.confidence,
        how="merged",
        manually_edited=True,
        block_no=pair.block_no,
    )
    sb = Pair(
        pair_key=new_id(),
        piece_id=piece_id,
        seq=pair.seq + 1,
        zh=body.zh_b,
        en=body.en_b,
        loc_page=pair.loc_page,
        needs_review=pair.needs_review,
        confidence=pair.confidence,
        how="merged",
        manually_edited=True,
        block_no=pair.block_no,
    )
    db.add_all([sa, sb])
    return _renumber_pairs(db, piece_id)


@router.post(
    "/projects/{project_id}/pieces/{piece_id}/pairs/merge",
    response_model=list[PairOut],
    summary="合并段落对（相邻两个合成一个）",
)
def merge_pair(
    project_id: str, piece_id: str, body: PairMergeIn, db: Session = Depends(get_db)
) -> list[Pair]:
    """把相邻两个段落对合并成一个（中文直接拼接，英文用空格连接）。

    F18 结构化编辑：对着背诵舱里断开的对句手动合并。新对拿**新的 ``pair_key``**，
    标 ``manually_edited``，覆盖重跑不冲。

    ``block_no`` 取**被合并两对中靠前那个**（ADR-0012）：合并后的内容横跨两个
    版面块时，按「首字符所在块」归属与 ``_attach_block_numbers`` 的口径一致 ——
    客户端按块号聚合成背诵单元，取靠前的块意味着这一单元以靠前的段为准，
    而不是被吞掉的那一段从列表里凭空消失。
    """
    a = _load_pair(db, project_id, piece_id, body.pair_key)
    b = _load_pair(db, project_id, piece_id, body.with_key)
    if abs(a.seq - b.seq) != 1:
        raise bad_request(ErrorCode.VALIDATION, "只能合并相邻的两个段落对")
    first, second = sorted([a, b], key=lambda p: p.seq)
    zh = (first.zh + second.zh).strip()
    en = (first.en + " " + second.en).strip()
    if not (zh and en):
        raise bad_request(ErrorCode.VALIDATION, "合并后的中英文不能为空")
    db.delete(a)
    db.delete(b)
    db.flush()
    db.add(
        Pair(
            pair_key=new_id(),
            piece_id=piece_id,
            seq=first.seq,
            zh=zh,
            en=en,
            loc_page=first.loc_page,
            needs_review=a.needs_review or b.needs_review,
            confidence=min(a.confidence, b.confidence),
            how="merged",
            manually_edited=True,
            block_no=first.block_no if first.block_no is not None else second.block_no,
        )
    )
    return _renumber_pairs(db, piece_id)


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

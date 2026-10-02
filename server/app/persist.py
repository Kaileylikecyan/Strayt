"""⑧ 入库：把对齐结果落成 ``Piece`` / ``Pair`` 行。

文档 §6 加工管线第 ⑧ 步。设计上守三条：

1. **人工编辑不能被静默冲掉。** F18 允许手动拆分/合并/交换/移动段落。覆盖重跑
   时若篇目里已有 ``manually_edited`` 的对，默认**跳过**并在返回值里说清原因；
   显式传 ``overwrite=True`` 才会覆盖，同时回报冲掉了几处手工改动，让界面能提示。
2. **一篇一个事务。** 篇目与其所有 ``Pair`` 要么全进要么全不进，不能留半篇。
3. **拒收的篇目不写任何行**，只把原因交回调用方（加工任务记进 ``failed_units``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.align.base import AlignResult
from app.align.quality import QualityReport
from app.db.models import Pair, Piece


class SaveOutcome(StrEnum):
    """入库结果。"""

    CREATED = "created"
    REPLACED = "replaced"
    SKIPPED = "skipped"
    REJECTED = "rejected"


@dataclass
class SaveResult:
    """一次篇目入库的结果。

    只回标量，**不回 ORM 实例**：调用方（加工任务）通常在 ``SessionLocal()``
    块外读这个返回值，而 session 一关 ORM 对象就 ``DetachedInstanceError``。
    需要篇目内容就拿 ``piece_id`` 再查。
    """

    outcome: SaveOutcome
    piece_id: str | None = None
    #: 为什么没写进去（``SKIPPED`` / ``REJECTED`` 时有值）
    reason: str | None = None
    #: 覆盖重跑时冲掉的人工改动条数
    wiped_edits: int = 0
    #: 标记为待核的对数
    review_count: int = 0
    #: 落库的 ⑦ 自检结论，便于调用方汇总
    report: QualityReport | None = None
    #: 整篇未入库时的失败单元明细
    failure: dict | None = field(default=None)


def find_piece(db: Session, project_id: str, file_id: str, title: str) -> Piece | None:
    """按 ``(项目, 文件, 标题)`` 找已有篇目。

    标题重复在同一文件里不该出现（③ 的结构识别按篇首行切），但真出现时取最早
    的一条，保持幂等行为稳定。
    """
    stmt = (
        select(Piece)
        .where(Piece.project_id == project_id, Piece.file_id == file_id, Piece.title == title)
        .order_by(Piece.created_at, Piece.sort_order)
        .limit(1)
    )
    return db.scalars(stmt).first()


def _manual_edit_count(db: Session, piece_id: str) -> int:
    return len(
        list(
            db.scalars(
                select(Pair.id).where(Pair.piece_id == piece_id, Pair.manually_edited.is_(True))
            )
        )
    )


def _next_sort_order(db: Session, project_id: str) -> int:
    """篇目排序号。取当前最大值 +1，删掉最后一篇后重跑不会与旧号撞车。"""
    last = db.scalars(
        select(Piece.sort_order)
        .where(Piece.project_id == project_id)
        .order_by(Piece.sort_order.desc())
        .limit(1)
    ).first()
    return (last + 1) if last is not None else 0


def save_piece(
    db: Session,
    *,
    project_id: str,
    file_id: str | None,
    title: str,
    res: AlignResult,
    report: QualityReport,
    align_mode: str,
    overwrite: bool = False,
    sort_order: int | None = None,
) -> SaveResult:
    """把一篇的 ``AlignResult`` 落库。**不提交事务**，由调用方决定。

    拒收（``report.verdict`` 为 ``rejected`` / ``failed``）时一行都不写。
    """
    if not res.pairs:
        return SaveResult(
            outcome=SaveOutcome.REJECTED,
            reason="没有可入库的段落对",
            report=report,
            failure={"unit": title, "reason": "no_pairs", "retryable": False},
        )
    if not report.ok:
        return SaveResult(
            outcome=SaveOutcome.REJECTED,
            reason=report.messages[0] if report.messages else "质量自检未通过",
            report=report,
            failure={
                "unit": title,
                "reason": "quality_gate",
                "retryable": False,
                "issues": [i.to_dict() for i in report.issues],
            },
        )

    existing = find_piece(db, project_id, file_id, title) if file_id else None

    if existing is not None and not overwrite:
        wiped = _manual_edit_count(db, existing.id)
        if wiped:
            return SaveResult(
                outcome=SaveOutcome.SKIPPED,
                piece_id=existing.id,
                reason=f"篇目内有 {wiped} 处人工微调，覆盖重跑会冲掉，需显式确认",
                wiped_edits=wiped,
                review_count=sum(1 for p in res.pairs if p.needs_review),
                report=report,
            )
        # 无人工改动：直接删掉旧的重建，比逐行 diff 简单且结果一致
        db.delete(existing)
        db.flush()
        outcome = SaveOutcome.REPLACED
    elif existing is not None:
        wiped = _manual_edit_count(db, existing.id)
        db.delete(existing)
        db.flush()
        outcome = SaveOutcome.REPLACED
    else:
        wiped = 0
        outcome = SaveOutcome.CREATED

    piece = Piece(
        project_id=project_id,
        file_id=file_id,
        title=title,
        align_mode=align_mode,
        align_warnings=report.messages,
        align_agreement=res.agreement,
        sort_order=sort_order if sort_order is not None else _next_sort_order(db, project_id),
    )
    db.add(piece)
    db.flush()  # 拿到 piece.id

    review = 0
    for draft in res.pairs:
        needs = bool(draft.needs_review)
        review += needs
        db.add(
            Pair(
                piece_id=piece.id,
                seq=draft.seq,
                zh=draft.zh,
                en=draft.en,
                loc_page=draft.loc_page,
                needs_review=needs,
                confidence=round(max(0.0, min(1.0, draft.confidence)), 3),
                how=draft.how,
                block_no=draft.block_no,
            )
        )
    db.flush()
    return SaveResult(
        outcome=outcome,
        piece_id=piece.id,
        wiped_edits=wiped,
        review_count=review,
        report=report,
    )


def rejection_failure(title: str, report: QualityReport) -> dict:
    """把拒收原因整理成 ``jobs.failed_units_json`` 的一行。"""
    return {
        "unit": title,
        "reason": "quality_gate",
        "retryable": False,
        "message": report.messages[0] if report.messages else "质量自检未通过",
        "verdict": str(report.verdict),
        "issues": [i.to_dict() for i in report.issues],
    }


__all__ = [
    "SaveOutcome",
    "SaveResult",
    "find_piece",
    "rejection_failure",
    "save_piece",
]

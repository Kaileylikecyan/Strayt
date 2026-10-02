"""给已有对句回填 ``pairs.block_no``（原文版面块归属，ADR-0012）。

**为什么不用重跑加工任务**：``block_no`` 是**纯机械推导**的 —— 重解析原文得到
逐字符块表，再用库里那句 ``pair.zh`` 去表里对齐位置，全程不碰 LLM。
重跑加工任务反而更贵也更危险：它会重新走 A/B 双通道对齐，LLM 重新吐一遍对齐结果，
用户手工改过的句子可能被覆盖（``manually_edited`` 的行尤其不该被冲）。

所以本脚本只**读**原文 + **写** ``block_no``，别的列一个字都不动。

    uv run python -m app.scripts.backfill_block_no --dry-run
    uv run python -m app.scripts.backfill_block_no
    uv run python -m app.scripts.backfill_block_no --project <project_id>

匹配不上的句子保持 ``NULL``，客户端会自动降级成按句（文档 F19.1），不猜。
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import File, Pair, Piece, Project
from app.db.session import SessionLocal
from app.jobs.engine import Unit, build_plan
from app.parse import blocks as B


def _attribute_piece(pairs: list[Pair], body: str, char_blocks: list[int] | None) -> int:
    """回填一个篇目的块号，返回成功命中的句子数。"""
    found = B.attribute_blocks([p.zh for p in pairs], body, char_blocks)
    renumber: dict[int, int] = {}
    hit = 0
    for pair, raw in zip(pairs, found, strict=True):
        pair.block_no = None
        if raw is None or raw < 0:
            continue
        if raw not in renumber:
            renumber[raw] = len(renumber)
        pair.block_no = renumber[raw]
        hit += 1
    return hit


def _backfill_project(db: Session, project: Project, dry_run: bool) -> tuple[int, int, int]:
    """回填一个项目的全部篇目，返回 (篇目数, 句子总数, 命中句子数)。"""
    files = list(
        db.scalars(select(File).where(File.project_id == project.id, File.deleted_at.is_(None)))
    )
    if not files:
        return (0, 0, 0)

    pieces = list(
        db.scalars(select(Piece).where(Piece.project_id == project.id).order_by(Piece.sort_order))
    )

    n_pieces = n_pairs = n_hit = 0
    for file_row in files:
        try:
            plan = build_plan(db, file_row)
        except (NotImplementedError, FileNotFoundError) as exc:
            print(f"  跳过 {file_row.original_name}：{exc}")
            continue

        by_title: dict[str, Unit] = {}
        for unit in plan.units:
            by_title.setdefault(unit.title, unit)

        for idx, piece in enumerate(pieces):
            if piece.file_id != file_row.id:
                continue
            unit = by_title.get(piece.title)
            if unit is None:
                # 标题被加工改过：退化成「按 sort_order 配」，同序号兜底
                unit = plan.units[idx] if idx < len(plan.units) else None
            if unit is None:
                print(f"  {piece.title[:24]}：找不到对应的解析单元，跳过")
                continue
            pairs = list(
                db.scalars(select(Pair).where(Pair.piece_id == piece.id).order_by(Pair.seq))
            )
            if not pairs:
                continue
            hit = _attribute_piece(pairs, unit.zh, unit.zh_blocks)
            n_pieces += 1
            n_pairs += len(pairs)
            n_hit += hit
            print(
                f"  {piece.title[:24]}：{hit}/{len(pairs)} 句拿到块号"
                + ("（全空 → 客户端按句）" if hit == 0 else "")
            )

    if not dry_run:
        db.commit()
    else:
        db.rollback()
    return (n_pieces, n_pairs, n_hit)


def main(argv: list[str] | None = None) -> int:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="给已有对句回填原文版面块号 block_no")
    ap.add_argument("--dry-run", action="store_true", help="只看结果，不写库")
    ap.add_argument("--project", help="只处理这一个项目 id")
    args = ap.parse_args(argv)

    with SessionLocal() as db:
        stmt = select(Project).where(Project.deleted_at.is_(None), Project.type == "recite")
        if args.project:
            stmt = stmt.where(Project.id == args.project)
        projects = list(db.scalars(stmt.order_by(Project.created_at)))
        if not projects:
            print("没有可回填的背诵型项目。")
            return 0

        total = [0, 0, 0]
        for project in projects:
            print(f"项目 {project.name}（{project.id}）")
            got = _backfill_project(db, project, args.dry_run)
            total = [a + b for a, b in zip(total, got, strict=True)]
            print(f"  小计：{got[0]} 篇目 / {got[1]} 句，命中 {got[2]} 句")

    verb = "将要回填" if args.dry_run else "已回填"
    print(f"\n{verb}：{total[0]} 篇目 / {total[1]} 句，命中 {total[2]} 句。")
    if total[2] < total[1]:
        print("部分句子匹配不上，保持 NULL，客户端会自动按句显示并提示。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

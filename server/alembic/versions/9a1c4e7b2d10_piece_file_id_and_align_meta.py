"""pieces 补 file_id / 对齐元信息，pairs 补 confidence / how

Revision ID: 9a1c4e7b2d10
Revises: 2570f8f8f8cc
Create Date: 2026-09-27

为什么必须补 ``pieces.file_id``：

``pairs.loc_page`` 单独存着「第 24 页」，但**页码离开文件就没有意义**。一期
设计里 ``pieces`` 只有 ``project_id``，于是：

- 二期「出处回看」拿不到该回看哪份资料的第 24 页；
- 加工任务断点续跑时不知道这批篇目出自哪份文件，重跑会写重复数据；
- 同一项目里放了两份同名的《外滩》范文也无法区分。

手工新建的篇目没有来源文件，所以列可空。

其余三列是为了让「对齐结果的可信度」能被客户端用起来，而不是只躺在服务端日志里：
- ``pieces.align_warnings``：降级、一致度过低、漏句等告警；
- ``pieces.align_agreement``：通道 A 与通道 B 的切分点一致度（F1），NULL 表示只走了通道 B；
- ``pairs.confidence`` / ``pairs.how``：让用户能按可疑程度排序排查。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9a1c4e7b2d10"
down_revision: str | None = "2570f8f8f8cc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pieces", sa.Column("file_id", sa.String(length=32), nullable=True))
    op.add_column("pieces", sa.Column("align_warnings", sa.JSON(), nullable=True))
    op.add_column("pieces", sa.Column("align_agreement", sa.Float(), nullable=True))
    op.add_column("pairs", sa.Column("confidence", sa.Float(), nullable=True))
    op.add_column("pairs", sa.Column("how", sa.String(length=16), nullable=True))

    # 历史行补上默认值：align_warnings 是 NOT NULL，confidence / how 同理
    op.execute("UPDATE pieces SET align_warnings = '[]' WHERE align_warnings IS NULL")
    op.execute("UPDATE pairs SET confidence = 1.0 WHERE confidence IS NULL")
    op.execute("UPDATE pairs SET how = 'direct' WHERE how IS NULL")

    op.alter_column("pieces", "align_warnings", nullable=False, existing_type=sa.JSON())
    op.alter_column("pairs", "confidence", nullable=False, existing_type=sa.Float())
    op.alter_column("pairs", "how", nullable=False, existing_type=sa.String(length=16))

    # 外键放最后建：pieces 已有存量数据，先补齐再挂约束
    op.create_foreign_key(
        "fk_pieces_file_id", "pieces", "files", ["file_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_pieces_file", "pieces", ["file_id"])


def downgrade() -> None:
    op.drop_index("ix_pieces_file", table_name="pieces")
    op.drop_constraint("fk_pieces_file_id", "pieces", type_="foreignkey")
    op.drop_column("pairs", "how")
    op.drop_column("pairs", "confidence")
    op.drop_column("pieces", "align_agreement")
    op.drop_column("pieces", "align_warnings")
    op.drop_column("pieces", "file_id")

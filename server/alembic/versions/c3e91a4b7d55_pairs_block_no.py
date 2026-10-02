"""pairs_block_no

Revision ID: c3e91a4b7d55
Revises: b91c4e70d5a8
Create Date: 2026-10-02 10:30:00.000000

给 ``pairs`` 加 ``block_no`` 列，支撑背诵舱的「按段」粒度（ADR-0012）。

**为什么加**（用户实测发现原文是一段段的，背诵舱却一句一句）：

原先「这句来自原文哪一段」这个信息在 ``parse/blocks.py::_flatten`` 被明确丢掉了
（docstring 原话：「块边界本身不保留」）。于是 ``pairs`` 只有句子，没有任何结构身份，
客户端想按原文段分组也无从下手 —— 唯一的选择是按字数硬拼，而拼出来的段并不等于原文段。

**为什么不改对齐粒度**（这才是关键，见 ADR-0012 决策 1）：

``pairs`` 保持句级原子。对齐若改成段级，中英两侧段数不等会让长度平衡 DP 整篇错位
（样板实测中 49 句 vs 英 55 句，本来就不等；``split_segments`` 的 docstring 记着
「49+54 句被压成 13 对」的事故）。**先句级对齐、再按块分组**是在已对齐正确的结果上做
合并，没有任何错位风险。

**NULL 的含义**：存量行全部为 NULL = 「这句不知道自己来自哪个块」。
客户端据此把「按段」**降级为「按句」并在界面标注**，而不是默默按字数拼一个假的段 ——
给用户一个假的段落划分比不给更糟。
存量篇目想要真段落必须**重跑加工任务**（会消耗 LLM 额度，用户已知并同意）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3e91a4b7d55"
down_revision: str | None = "b91c4e70d5a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 可空且**不给 server_default**：NULL 有确切含义（见上方 docstring），
    # 给个 0 之类的默认值会把「没加工过」伪装成「第 0 块」。
    op.add_column("pairs", sa.Column("block_no", sa.Integer(), nullable=True))
    op.create_index("ix_pairs_piece_block", "pairs", ["piece_id", "block_no"])


def downgrade() -> None:
    op.drop_index("ix_pairs_piece_block", table_name="pairs")
    op.drop_column("pairs", "block_no")

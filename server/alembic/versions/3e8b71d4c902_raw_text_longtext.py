"""file_parses.raw_text 改 LONGTEXT

Revision ID: 3e8b71d4c902
Revises: 9a1c4e7b2d10
Create Date: 2026-09-27

为什么非改不可：``raw_text`` 原来是 MySQL ``TEXT``，上限 65,535 字节。
样板 PDF（``fixtures/daoyouci.pdf``，24 页）清洗后是 159,817 字节 —— 超了一倍多。

也就是说**任何真实 PDF 都会在 ``build_plan`` 写 ``FileParse`` 时报
``DataError 1406 Data too long``**，整条加工链路根本走不完。之前一直没暴露，
是因为单元测试的解析样本都是几十字节的小段，真实数据只走了纯函数
（``var_test/quality_e2e.py`` 不落库）。

同一个坑还有一处潜在风险：``pages_json`` 样板是 179KB。MySQL 的 ``JSON``
列底层是 LONGBLOB，放得下，但要求 ``max_allowed_packet`` 至少几 MB
（8.0 默认 64MB，无需处理），所以这里不动它。

顺带把 ``pairs.zh`` / ``pairs.en`` 也提到 LONGTEXT：实测最长段 8.4KB 仍远小于
``TEXT`` 上限，但一份版式极差的资料可能把整页塞进一段，留个余量比事后
``DataError`` 便宜。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "3e8b71d4c902"
down_revision: str | None = "9a1c4e7b2d10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LONGTEXT = sa.Text().with_variant(mysql.LONGTEXT(), "mysql")


def upgrade() -> None:
    op.alter_column(
        "file_parses",
        "raw_text",
        existing_type=sa.Text(),
        type_=_LONGTEXT,
        existing_nullable=True,
    )
    for col in ("zh", "en"):
        op.alter_column(
            "pairs",
            col,
            existing_type=sa.Text(),
            type_=_LONGTEXT,
            existing_nullable=False,
        )


def downgrade() -> None:
    for col in ("zh", "en"):
        op.alter_column(
            "pairs",
            col,
            existing_type=_LONGTEXT,
            type_=sa.Text(),
            existing_nullable=False,
        )
    op.alter_column(
        "file_parses",
        "raw_text",
        existing_type=_LONGTEXT,
        type_=sa.Text(),
        existing_nullable=True,
    )

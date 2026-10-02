"""category_rules_priority

Revision ID: d4f1a8b6c207
Revises: 6e5c83c7e97f
Create Date: 2026-09-30 12:10:00.000000

给 ``category_rules`` 加显式 ``priority`` 列。

**为什么必须加**（这是个真 bug，不是重构洁癖）：

原先「规则优先级 = ``id`` 升序」的做法根本不成立。``id`` 由 ``new_id()`` 随机生成
（uuid4 hex），所以回读顺序与用户提交的数组顺序毫无关系。后果：

* 用户精心把「更精确的规则」排在前面，实际生效的却是随机那一条；
* ``smoke_http.py`` 里 ``got[0]["pattern"] == ...`` 的断言是**碰运气**通过的
  —— 本轮加口令登录后重跑就翻车了。

已存在的行无法还原出用户当初的意图（那信息从来没被存下来），统一给 0；
新写入的按提交数组下标落库。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f1a8b6c207"
down_revision: str | None = "6e5c83c7e97f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "category_rules",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    )
    # 联合索引：列表按 (project_id, priority) 取，分类匹配也按这个顺序扫第一条命中。
    op.create_index(
        "ix_category_rules_project_priority", "category_rules", ["project_id", "priority"]
    )
    # server_default 用完就撤：让后续 INSERT 必须显式给值，
    # 免得又出现「忘了写 priority → 全落 0 → 顺序错乱」这类静默 bug。
    op.alter_column("category_rules", "priority", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_category_rules_project_priority", table_name="category_rules")
    op.drop_column("category_rules", "priority")

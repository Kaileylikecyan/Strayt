"""F11 图谱抽取：给 ``jobs`` 加 ``results_json`` 存预览草稿。

图谱抽取（``graph_extract``）与背诵对齐不同：结果先落草稿等客户端预览确认，
确认后才写正式表。草稿存在任务的 ``results_json``（整体替换触发脏标记，
engine 从不原地改嵌套 dict）。

Revision ID: a7c2e01f3b05
Revises: 3e8b71d4c902
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7c2e01f3b05"
down_revision = "3e8b71d4c902"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("results_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "results_json")

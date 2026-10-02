"""api_keys_base_url

Revision ID: b91c4e70d5a8
Revises: d4f1a8b6c207
Create Date: 2026-09-30 13:20:00.000000

给 ``api_keys`` 加 ``base_url`` 列，支撑「自定义 OpenAI 兼容端点」这一档。

背景：接入清单里已经有国内主流几家（月之暗面 / 豆包 / 硅基流动 / MiniMax / 混元），
但个人自用场景里还有一类 Key 是**注册表列不出来**的：本地 vLLM / Ollama /
LM Studio，以及 OneAPI / NewAPI 这类聚合中转。它们都只提供一个 OpenAI 兼容
的 ``/chat/completions``，却各有各的地址。之前这条路径不存在，用户只能去改代码。

安全约束（在 API 层强制，见 ``settings.py::create_key``）：
固定端点的厂商**不允许**填这列。放开的风险是 —— 用户（或误操作）把某个 Key 的
``base_url`` 指到别的主机，Key 就会跟着 Authorization 头一起发过去。
所以这一列只对 ``provider='openai_compatible'`` 开放。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b91c4e70d5a8"
down_revision: str | None = "d4f1a8b6c207"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 存量行全是固定端点厂商，base_url 理应为空。逐行 UPDATE 不写数据 ——
    # 加列默认就是 NULL，写一遍只会让这张表多出无意义的 redo。
    op.add_column("api_keys", sa.Column("base_url", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "base_url")

"""files_dedup_per_project

Revision ID: c7e14b83a2d9
Revises: b39d0fcb295f
Create Date: 2026-09-29 14:40:00.000000

把 ``files`` 的去重唯一键从全局 ``sha256`` 改成 ``(project_id, sha256)``。
磁盘实体仍然只存一份（相对路径按 sha256 分配），但同一份资料能挂到多个项目。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c7e14b83a2d9"
down_revision: str | None = "b39d0fcb295f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_files_sha256", "files", type_="unique")
    op.create_unique_constraint("uq_files_project_sha256", "files", ["project_id", "sha256"])


def downgrade() -> None:
    op.drop_constraint("uq_files_project_sha256", "files", type_="unique")
    op.create_unique_constraint("uq_files_sha256", "files", ["sha256"])

"""job_interrupted_status

Revision ID: b39d0fcb295f
Revises: a7c2e01f3b05
Create Date: 2026-09-28 15:53:58.191342
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b39d0fcb295f"
down_revision: str | None = "a7c2e01f3b05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # F8 超支熔断新增终态。MySQL 8 的 CHECK 约束不进 autogenerate 对比，手写。
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint(
        "ck_jobs_status",
        "jobs",
        "status in ('queued','running','success','failed','cancelled','interrupted')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint(
        "ck_jobs_status",
        "jobs",
        "status in ('queued','running','success','failed','cancelled')",
    )

"""请求/响应模型。

约定：
- 入参用 ``*In``，出参用 ``*Out``，一律独立声明。
- 所有出参时间字段为 **naive UTC**，客户端本地化时区显示。
- ``updated_at`` 在所有可写实体上出现（弱同步裁决依据）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ProjectType = Literal["graph", "recite"]
AlignMode = Literal["llm", "regular"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ==========================================================================
# 项目
# ==========================================================================
class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: ProjectType
    #: 绑定的模型档案 id（对应 model_profiles.id），可空表示用默认档案
    api_profile_id: str | None = None


class ProjectOut(ORMModel):
    id: str
    name: str
    type: ProjectType
    api_profile_json: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class GoalOut(ORMModel):
    id: str
    project_id: str
    exam_date: date
    updated_at: datetime


class GoalIn(BaseModel):
    exam_date: date


# ==========================================================================
# 文件
# ==========================================================================
class FileOut(ORMModel):
    id: str
    project_id: str
    orig_name: str
    sha256: str
    size: int
    parse_channel: Literal["text", "vision"]
    page_count: int | None = None
    uploaded_at: datetime
    deleted_at: datetime | None = None


class UploadSessionCreate(BaseModel):
    """客户端先声明文件指纹，服务端据此判断走直传还是分块。"""

    orig_name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UploadSessionOut(ORMModel):
    id: str
    project_id: str
    orig_name: str
    size: int
    sha256: str
    chunk_size: int
    total_chunks: int
    received_json: dict[str, Any]
    status: Literal["pending", "complete", "aborted"]
    created_at: datetime
    updated_at: datetime


# ==========================================================================
# 背诵舱
# ==========================================================================
class PairOut(ORMModel):
    pair_key: str
    seq: int
    zh: str
    en: str
    loc_page: int | None = None
    needs_review: bool
    manually_edited: bool
    updated_at: datetime


class PieceOut(ORMModel):
    id: str
    project_id: str
    title: str
    align_mode: AlignMode
    recited: bool
    last_pos: int
    sort_order: int
    pair_count: int = 0
    updated_at: datetime


class PieceProgressIn(BaseModel):
    """背诵舱进度。客户端可离线写，联网后随写队列上传。"""

    last_pos: int = Field(ge=0)
    recited: bool | None = None
    client_ts: datetime


# ==========================================================================
# 加工任务
# ==========================================================================
JobStatus = Literal["queued", "running", "success", "failed", "cancelled", "interrupted"]


class JobCreate(BaseModel):
    """新建加工任务。``recite_align`` 背诵对齐；``graph_extract`` 图谱抽取。"""

    project_id: str
    file_id: str
    type: Literal["recite_align", "graph_extract"] = "recite_align"
    #: 覆盖重跑。篇目内有人工微调（F18 的拆分/合并/交换/移动）时，
    #: 不带这个标志会**跳过**并说明原因，而不是静默冲掉用户的改动。
    overwrite: bool = False


class JobOut(ORMModel):
    id: str
    project_id: str
    type: str
    status: JobStatus
    progress: int
    total_units: int
    done_units: int
    error: str | None = None
    failed_units_json: list[Any]
    checkpoint_json: dict[str, Any]
    tokens_used: int
    cost_estimate_json: dict[str, Any] | None = None
    units_json: list[Any]
    created_at: datetime
    updated_at: datetime
    heartbeat_at: datetime


# ==========================================================================
# 打卡 / 计划
# ==========================================================================
class CheckinIn(BaseModel):
    date: date
    items: list[str] = Field(default_factory=list)


class CheckinOut(ORMModel):
    date: date
    items_json: list[str]
    updated_at: datetime


class PlanIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    due_date: date | None = None
    status: Literal["todo", "doing", "done"] = "todo"
    target_type: Literal["category", "piece"] | None = None
    target_id: str | None = None
    sort_order: int = 0


class PlanPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    due_date: date | None = None
    status: Literal["todo", "doing", "done"] | None = None
    sort_order: int | None = None


class PlanOut(ORMModel):
    id: str
    project_id: str
    title: str
    due_date: date | None
    status: Literal["todo", "doing", "done"]
    target_type: str | None
    target_id: str | None
    sort_order: int
    created_at: datetime
    updated_at: datetime


class PlanListOut(PlanOut):
    """阶段计划列表项：在 PlanOut 上附加相对参考日的派生字段。"""

    days_until: int | None = None
    overdue: bool = False

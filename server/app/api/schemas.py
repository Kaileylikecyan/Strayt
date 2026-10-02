"""请求/响应模型。

约定：
- 入参用 ``*In``，出参用 ``*Out``，一律独立声明。
- 所有出参时间字段为 **naive UTC**，客户端本地化时区显示。
- ``updated_at`` 在所有可写实体上出现（弱同步裁决依据）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.security import validate_password_strength

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
    #: 原文版面块序号，篇目内 0 起稠密（ADR-0012）。背诵舱「按段」粒度按它聚合成
    #: 背诵单元；``None`` = 无块身份，客户端降级为「按句」并标注。
    #: 未重跑加工的存量篇目恒为 ``None``。
    block_no: int | None = None
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


class PairSplitIn(BaseModel):
    """把一个段落对拆成两个。两半中英文都由客户端算好（F18 结构化编辑）。"""

    pair_key: str
    zh_a: str = Field(min_length=1)
    en_a: str = Field(min_length=1)
    zh_b: str = Field(min_length=1)
    en_b: str = Field(min_length=1)


class PairMergeIn(BaseModel):
    """把相邻两个段落对合并成一个。``with_key`` 必须与 ``pair_key`` 相邻（F18）。"""

    pair_key: str
    with_key: str


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


# ==========================================================================
# 访问口令登录（单用户免账号，决策 D-01 / ADR-0009）
# ==========================================================================
class AuthStateOut(BaseModel):
    """免鉴权的准入状态探测。

    客户端靠它决定首屏是「创建口令」还是「输入口令」—— 两者的区别只是
    ``POST /auth/setup`` 与 ``POST /auth/login``，不必靠错误码反推。
    """

    password_set: bool


class PasswordIn(BaseModel):
    """新口令输入。

    强度校验放在**这里**（pydantic 字段校验器）而不是 ``hash_password`` 里：
    在路由函数体里抛 ``ValueError`` 会变成 500，那是服务器错误不是客户端错误。
    走校验器则自动 422，且 ``ctx`` 里的异常对象由 ``jsonable_errors`` 兜住
    （见 ``app/core/errors.py`` 里那个 422 序列化的坑）。
    """

    password: str = Field(min_length=1, max_length=1024)

    @field_validator("password")
    @classmethod
    def _strength(cls, v: str) -> str:
        validate_password_strength(v)
        return v


class PasswordChangeIn(BaseModel):
    """改口令输入。``old_password`` 只校验非空 —— 强度是**当前**口令的事，
    强制它满足现行强度规则会让「弱口令时代设的旧口令」永远改不了。
    """

    old_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)

    @field_validator("new_password")
    @classmethod
    def _strength(cls, v: str) -> str:
        validate_password_strength(v)
        return v


class SessionOut(BaseModel):
    """登录/创建口令成功后下发的会话令牌。

    令牌是 HMAC 签名（不是随机串），因此服务端无需会话表；它的有效期由
    签发时的 TTL 决定，改口令会让它立刻失效。**只在响应里出现，不写日志。**
    """

    session_token: str
    expires_at: datetime

"""SQLAlchemy 2.0 ORM 模型。

对应 `docs/学习工作台产品说明文档.md` §9.2 字段级设计，另有 5 张补充表与若干补字段，
补充原因逐条写在对应模型的 docstring 里。

约定
----
- **时间统一存 naive UTC**。MySQL DATETIME 不带时区，混用会出事。
  ``utcnow()`` 是唯一的写入口。
- **枚举用 VARCHAR + CHECK** 而非 MySQL 原生 ENUM。原生 ENUM 加值要
  ``ALTER TABLE ... MODIFY COLUMN``，Alembic 迁移很别扭；CHECK 同样能挡住脏数据，
  且单元测试可用 SQLite 跑。
- **每个可被客户端写入的实体都带 ``updated_at``**：弱同步（F2）以它做时间戳裁决。
  文档原设计里 ``pieces`` / ``pairs`` / ``checkins`` 缺这个字段，是必须补的。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """naive UTC 当前时间。全仓唯一时间来源。"""
    return datetime.now(UTC).replace(tzinfo=None)


def new_id() -> str:
    """客户端可预生成的可寻址 ID。写队列按它寻址，不依赖自增顺序。"""
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


# ==========================================================================
# 一期主用
# ==========================================================================
class Project(Base):
    """学习项目。type 创建后不可更改（文档 F4 规则），所以不做 update 入口。"""

    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("type in ('graph','recite')", name="ck_projects_type"),
        Index("ix_projects_deleted", "deleted_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 绑定的加工模型档案 id（文本/视觉分别指定，见 model_profiles）
    api_profile_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    # 回收站：软删 + 保留 N 天（文档 §9.4）
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    files: Mapped[list[File]] = relationship(back_populates="project", cascade="all, delete-orphan")
    pieces: Mapped[list[Piece]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Piece.sort_order"
    )
    goal: Mapped[Goal | None] = relationship(
        back_populates="project", cascade="all, delete-orphan", uselist=False
    )


class File(Base):
    """上传到服务端的原始资料。DB 只存相对路径 + SHA-256，实体在磁盘上。"""

    __tablename__ = "files"
    __table_args__ = (
        CheckConstraint("parse_channel in ('text','vision')", name="ck_files_channel"),
        # 同内容去重（文档 §13「文件去重（SHA-256）」）：磁盘实体只存一份，
        # 但去重范围是**项目内** —— 同一份资料要能挂到多个项目（换个科目再学一遍），
        # 所以唯一键是 (project_id, sha256)，不能是全局 sha256。
        UniqueConstraint("project_id", "sha256", name="uq_files_project_sha256"),
        Index("ix_files_project", "project_id"),
        Index("ix_files_deleted", "deleted_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # 相对 data_dir 的路径，永不存绝对路径（换机迁移不失效）
    server_path: Mapped[str] = mapped_column(String(512), nullable=False)
    orig_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parse_channel: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    # 原始 PDF 页数。出处回看（二期）依赖它
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    project: Mapped[Project] = relationship(back_populates="files")
    parses: Mapped[list[FileParse]] = relationship(
        back_populates="file", cascade="all, delete-orphan", order_by="FileParse.created_at"
    )


class Piece(Base):
    """背诵型项目的一篇（篇目）。

    补字段 ``updated_at``：文档原设计缺失，但弱同步（F2）要求所有客户端可写实体
    都有时间戳用于裁决。

    补字段 ``file_id``：``pairs.loc_page`` 单独存在**没有意义** —— 「第 24 页」
    必须相对于某个文件才能解析。缺了这一列，二期出处回看无从下手，加工任务
    断点续跑也不知道这批篇目来自哪份资料。
    """

    __tablename__ = "pieces"
    __table_args__ = (
        CheckConstraint("align_mode in ('llm','regular')", name="ck_pieces_align_mode"),
        Index("ix_pieces_project_sort", "project_id", "sort_order"),
        Index("ix_pieces_file", "file_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # 来源文件。手工新建的篇目没有来源，故可空。
    file_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("files.id", ondelete="CASCADE"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    align_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="llm")
    # 加工过程中的告警（降级、一致度过低、漏句等），要让用户看得见，
    # 不能只打在服务端日志里
    align_warnings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 通道 A 与通道 B 的切分点一致度（F1）。为 NULL 表示只走了通道 B。
    align_agreement: Mapped[float | None] = mapped_column(Float, nullable=True)
    recited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 「上次背到」：第几对（0 起）
    last_pos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="pieces")
    pairs: Mapped[list[Pair]] = relationship(
        back_populates="piece", cascade="all, delete-orphan", order_by="Pair.seq"
    )


class Pair(Base):
    """段落对 ``{zh, en}``。交错背诵舱的最小渲染单元。

    补两个字段：
    - ``pair_key``：手动微调（F18 的拆分/合并/交换/移动）会改 ``seq``，
      所以 ``(piece_id, seq)`` 不能当写队列的寻址键。改用稳定 UUID。
    - ``updated_at``：同上，时间戳裁决用。
    """

    __tablename__ = "pairs"
    __table_args__ = (
        UniqueConstraint("pair_key", name="uq_pairs_pair_key"),
        # 文档 §9.2 要求的复合索引
        Index("ix_pairs_piece_seq", "piece_id", "seq"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    pair_key: Mapped[str] = mapped_column(String(32), nullable=False, default=new_id)
    piece_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("pieces.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # LONGTEXT：实测最长段 8.4KB 还没撑爆 TEXT，但版式差的资料可能把整页塞进
    # 一段。与 ``file_parses.raw_text`` 同批升级，避免以后再吃一次 DataError 1406。
    zh: Mapped[str] = mapped_column(
        Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False, default=""
    )
    en: Mapped[str] = mapped_column(
        Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False, default=""
    )
    # 出处页码（0 起）。文档 §6 加工管线不变量：一期必须保留，二期出处回看才不用重跑
    loc_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 对齐置信度低 / 需人工核对，由通道 A 标记
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 对齐置信度 0~1。``needs_review`` 只说「要看一眼」，这个值让客户端能排序，
    # 用户可以先看最可疑的几对而不必逐条翻。
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    # 这一对是怎么对齐出来的：direct | merged | llm。排查错位时用得上。
    how: Mapped[str] = mapped_column(String(16), nullable=False, default="direct")
    # 人工微调过（用于"覆盖重跑"时提示哪些改动会被冲掉）
    manually_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    piece: Mapped[Piece] = relationship(back_populates="pairs")


class Goal(Base):
    """项目目标：考试日期倒计时（文档 F22 的一期最小集）。"""

    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    exam_date: Mapped[date] = mapped_column(Date, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="goal")


class Checkin(Base):
    """每日打卡。``items_json`` 是打卡项 id 数组（用户可自定义打卡项）。"""

    __tablename__ = "checkins"
    __table_args__ = (CheckConstraint("json_valid(items_json)", name="ck_checkins_json"),)

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    items_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class Plan(Base):
    """阶段计划（文档 F22）。一期只做简单待办，不做逾期顺延。"""

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("status in ('todo','doing','done')", name="ck_plans_status"),
        Index("ix_plans_project_due", "project_id", "due_date"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="todo")
    # 关联到一级分类或篇目
    target_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class Setting(Base):
    """KV 配置。键固定如下：

    - ``access_token_hash``      访问令牌哈希（唯一准入凭证）
    - ``schema_version``         快照 schema 版本，客户端据此判断本地缓存是否失效
    - ``checkin_items``          每日打卡项定义（用户可自定义）
    - ``default_model_profile``  默认模型档案 id
    """

    __tablename__ = "settings"

    k: Mapped[str] = mapped_column(String(64), primary_key=True)
    v: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


# ==========================================================================
# 加工任务（文档 §5「加工任务」，可断点续跑）
# ==========================================================================
class Job(Base):
    """一次管线执行实例。客户端可关窗，服务端后台跑完。

    补字段：``checkpoint_json``（断点续跑）、``tokens_used`` / ``cost_estimate_json``
    （客户端要展示"已耗 token"与成本确认弹窗，文档 §8.3）。
    """

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','success','failed','cancelled','interrupted')",
            name="ck_jobs_status",
        ),
        Index("ix_jobs_project_created", "project_id", "created_at"),
        Index("ix_jobs_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # 'recite_align' | 'graph_extract'（二期预留）| 'vision_ocr'
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    # 0~100
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 总单元数与已完成单元数，用于"逐章进度"
    total_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 失败单元明细：[{unit, reason, retryable}]
    # 这两列会被加工引擎**原地改**（逐单元标状态、逐条追加失败），
    # 所以必须用 MutableList 追踪脏；普通 JSON 列的原地改动会被静默丢弃，
    # 表现为任务跑完了但 units_json 全是 pending。
    failed_units_json: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON), nullable=False, default=list
    )
    # 断点：{file_id, done: [...], failed: [...]}（``done`` 存 ``index:title`` 稳定 key）
    checkpoint_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_estimate_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 单元级明细，供客户端渲染"逐章进度 / 失败章标红"
    units_json: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON), nullable=False, default=list
    )
    # F11 图谱抽取的预览草稿：{"units": {篇key: GraphUnit.to_dict()},
    # "draft": GraphDraft.to_dict(), "status": "pending_confirm"}。
    # 抽取只写这里，客户端确认后才落正式表。整体替换触发脏标记，
    # 不要原地改嵌套 dict。
    results_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    # 心跳：服务重启后据此识别僵死任务
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


# ==========================================================================
# 补充表（文档 §9.2 未覆盖，但一期功能必需）
# ==========================================================================
class UploadSession(Base):
    """分块上传会话（文档 F9 要求 >50MB 分块 + 断点续传）。

    没有这张表，服务端无法知道客户端已传了哪些块，续传只能靠客户端自觉。
    """

    __tablename__ = "upload_sessions"
    __table_args__ = (Index("ix_upload_sessions_status", "project_id", "status"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    orig_name: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    received_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # pending | complete | aborted
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class FileParse(Base):
    """解析产物留存（文档 §7 流程 2：全部失败时"保留原始解析结果，支持换模型重试"）。

    ``raw_text`` 存版面还原后的全文，``pages_json`` 存逐页分段结果（含页号），
    页号是二期「出处回看」的唯一数据来源，一期丢了就只能重跑全部加工。
    """

    __tablename__ = "file_parses"
    __table_args__ = (Index("ix_file_parses_file", "file_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    file_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    # text_extracted | vision_ocr
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    # success | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    # 必须是 LONGTEXT：样板 PDF 清洗后就有 ~16 万字节，而 MySQL 的 TEXT 上限 65,535。
    # 用 TEXT 的话任何真实资料都会在这里 ``DataError 1406`` 直接失败 —— 之前没炸是
    # 因为测试样本都是几十字节的小段。``with_variant`` 让别的方言仍用 TEXT。
    raw_text: Mapped[str | None] = mapped_column(
        Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=True
    )
    # [{"page": 0, "lines": [...], "paragraphs": [...]}]
    pages_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    file: Mapped[File] = relationship(back_populates="parses")


class ApiKey(Base):
    """LLM 服务商 API Key。密文存储（Fernet），同服务商可存多个（文档 F6）。

    文档原设计把 Key 塞进 ``settings`` KV 表，无法支持"同一服务商多个 Key"，故拆表。
    """

    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_provider", "provider", "enabled"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    # 掩码，展示用，永不回传完整 Key
    secret_mask: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 最近一次连通性测试结果
    last_test_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class ModelProfile(Base):
    """加工用的服务商 + 模型绑定，文本与视觉分别指定（文档 F7）。

    文档原设计只有 ``projects.api_profile_json``，但一个档案要同时描述文本与视觉
    两个能力位，抽成表才能在设置中心复用与切换。

    注：MySQL 禁止「参与 CHECK 约束的列」同时带 ``ON DELETE`` 引用动作，所以
    「至少绑定一个能力位」这条约束放到 API 层（``app/api/routers/settings.py``），
    不写成 CHECK。
    """

    __tablename__ = "model_profiles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    text_provider_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    text_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vision_provider_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    vision_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


# ==========================================================================
# 图谱型（二期）。一期建表不写入，为二期预留，避免再改表结构。
# ==========================================================================
class Category(Base):
    """一级分类（图谱顶层分组）。"""

    __tablename__ = "categories"
    __table_args__ = (Index("ix_categories_project_sort", "project_id", "sort_order"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Node(Base):
    """知识点（图谱节点）。"""

    __tablename__ = "nodes"
    __table_args__ = (
        CheckConstraint("mastery in ('no','mid','yes')", name="ck_nodes_mastery"),
        CheckConstraint("weight between 1 and 3", name="ck_nodes_weight"),
        # 文档 §9.2 要求的复合索引
        Index("ix_nodes_project_category", "project_id", "category_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 重要度 1~3，决定节点大小
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    # LLM 提炼概要；不得超出原文
    card_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 用户手写笔记（文档禁止通用笔记编辑器，只留有限手动编辑）
    user_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 卡片是否被人工编辑过（编辑内容与 AI 内容分区展示）
    edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mastery: Mapped[str] = mapped_column(String(8), nullable=False, default="no")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class CardQuote(Base):
    """知识卡片的原文关键句 + 出处引用。``page``/``para`` 是二期出处回看的落点。"""

    __tablename__ = "card_quotes"
    __table_args__ = (Index("ix_card_quotes_node", "node_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    quote_text: Mapped[str] = mapped_column(Text, nullable=False)
    file_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    para: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Edge(Base):
    """知识点关联。``reason`` 是 LLM 依据原文给出的关联依据（文档要求必须标注）。"""

    __tablename__ = "edges"
    __table_args__ = (
        UniqueConstraint("from_node", "to_node", name="uq_edges_pair"),
        Index("ix_edges_project", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    from_node: Mapped[str] = mapped_column(
        String(32), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    to_node: Mapped[str] = mapped_column(
        String(32), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Flashcard(Base):
    """闪卡自测（二期，D-02 内嵌图谱型，不设独立模式）。"""

    __tablename__ = "flashcards"
    __table_args__ = (Index("ix_flashcards_node", "node_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

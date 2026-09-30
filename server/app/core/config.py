"""应用配置。

所有配置项均可通过环境变量或 server/.env 覆盖，前缀 ``STRAYT_``。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# server/ 目录（本文件位于 server/app/core/）
SERVER_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STRAYT_",
        env_file=(SERVER_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 基础 ----
    app_name: str = "Strayt 学习工作台"
    server_name: str = "strayt-local"
    debug: bool = False

    # ---- 数据目录 ----
    # var/ 存放运行期产物：数据库连接、文件存储、回收站、备份、解析中间件
    data_dir: Path = SERVER_ROOT / "var"

    # ---- 数据库 ----
    # 默认指向本机 Docker MySQL 8；用 docker-compose 起的实例
    db_url: str = "mysql+pymysql://strayt:strayt@127.0.0.1:3306/strayt?charset=utf8mb4"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 1800

    # ---- 安全 ----
    # 访问令牌哈希用的 pepper。留空则启动时随机生成（重启后旧令牌失效，仅限本地开发）
    access_token_pepper: str = ""
    # API Key 加密用的主密钥（Fernet）。生产必须显式配置
    secret_key: str = ""

    # ---- 存储 ----
    # 上传文件的分块大小（字节）。5MB
    upload_chunk_size: int = 5 * 1024 * 1024
    # 超过此大小必须分块上传并支持断点续传
    upload_chunk_threshold: int = 50 * 1024 * 1024
    # 回收站保留天数
    recycle_retention_days: int = 7

    # ---- 加工 ----
    # 单个 job 的工作线程数上限
    job_max_workers: int = 2
    # 加工任务心跳超时（秒），超时视为僵死并可重跑
    job_heartbeat_timeout: int = 120
    # LLM 调用默认超时（秒）
    llm_timeout: int = 90
    # API Key 连通性测试超时（秒）——文档 F6 要求 10s 内返回
    llm_probe_timeout: int = 10

    # ---- 弱同步 ----
    # 快照接口返回的服务端 schema 版本，客户端据此判断本地缓存是否失效
    snapshot_schema_version: int = Field(default=1, ge=1)
    # 写队列单次重放的最大条数
    sync_batch_limit: int = 200

    @field_validator("data_dir", mode="before")
    @classmethod
    def _abs_data_dir(cls, v: str | Path) -> Path:
        p = Path(v)
        return p if p.is_absolute() else (SERVER_ROOT / p)

    # ---- 派生路径 ----
    @property
    def storage_dir(self) -> Path:
        """原始资料文件存储目录（原子上传落点）。"""
        return self.data_dir / "storage"

    @property
    def recycle_dir(self) -> Path:
        """回收站：删项目/删文件后暂存，保留 N 天。"""
        return self.data_dir / "recycle"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backup"

    @property
    def tmp_dir(self) -> Path:
        """分块上传的临时分片目录。"""
        return self.data_dir / "tmp"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.storage_dir, self.recycle_dir, self.backup_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s

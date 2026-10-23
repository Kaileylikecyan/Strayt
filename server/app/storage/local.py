"""磁盘文件存储。

规矩（架构红线 3）：
- 原始文件存服务端磁盘，DB 只存**相对** ``data_dir`` 的路径 + SHA-256。
- 同内容去重：SHA-256 命中则复用已有实体，不重复占盘。
- 删项目/删文件不是真删：移入回收站目录，保留 N 天后由清理任务彻底删除。
- 写入必须是「临时文件 + 原子 rename」，避免中断留下半个文件被当成有效资料。
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.config import get_settings
from app.db.models import utcnow

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(_CHUNK):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class StoredFile:
    relative_path: str
    size: int
    sha256: str


class Storage:
    """``data_dir/storage`` 之上的薄封装。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.root = self.settings.storage_dir
        self.recycle = self.settings.recycle_dir
        self.tmp = self.settings.tmp_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.recycle.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    # ---- 路径 ----
    def abs_path(self, relative: str) -> Path:
        p = (self.root / relative).resolve()
        # 防目录穿越：解析后必须仍在 storage 根内
        if not p.is_relative_to(self.root.resolve()):
            raise ValueError(f"路径越界: {relative}")
        return p

    def alloc_relpath(self, sha256: str, ext: str) -> str:
        """按 SHA-256 前两位分桶，避免单目录塞几十万文件。

        同内容天然落到同一路径 → 重复上传直接命中去重。
        """
        ext = ext if ext and len(ext) <= 12 else ".bin"
        return f"{sha256[:2]}/{sha256[2:4]}/{sha256}{ext.lower()}"

    # ---- 写入 ----
    def write_atomic(self, relpath: str, data: bytes) -> StoredFile:
        target = self.abs_path(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = target.with_suffix(target.suffix + ".part")
        stage.write_bytes(data)
        stage.replace(target)  # 同卷 rename，原子
        return StoredFile(
            relative_path=relpath, size=target.stat().st_size, sha256=sha256_file(target)
        )

    def write_stream_atomic(self, relpath: str, chunks: Iterator[bytes]) -> StoredFile:
        target = self.abs_path(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = target.with_suffix(target.suffix + ".part")
        total = 0
        with stage.open("wb") as f:
            for c in chunks:
                f.write(c)
                total += len(c)
        stage.replace(target)
        return StoredFile(relative_path=relpath, size=total, sha256=sha256_file(target))

    def concat_chunks(self, ordered_tmp_files: list[Path], relpath: str) -> StoredFile:
        """把分片按序拼成完整文件（走临时文件 + 原子 rename）。"""
        target = self.abs_path(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = target.with_suffix(target.suffix + ".part")
        with stage.open("wb") as out:
            for part in ordered_tmp_files:
                with part.open("rb") as f:
                    shutil.copyfileobj(f, out, _CHUNK)
        stage.replace(target)
        return StoredFile(
            relative_path=relpath, size=target.stat().st_size, sha256=sha256_file(target)
        )

    # ---- 分块上传临时区 ----
    def session_dir(self, session_id: str) -> Path:
        d = self.tmp / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def chunk_path(self, session_id: str, index: int) -> Path:
        return self.session_dir(session_id) / f"{index:06d}.part"

    def drop_session(self, session_id: str) -> None:
        shutil.rmtree(self.tmp / session_id, ignore_errors=True)

    # ---- 删除 / 回收站 ----
    def to_recycle(self, relative: str, project_id: str) -> str | None:
        """把文件移入回收站，返回回收站内的相对路径。文件不存在时返回 None。"""
        src = self.abs_path(relative)
        if not src.exists():
            return None
        stem, suffix, stamp = src.stem, src.suffix, utcnow().strftime("%Y%m%d-%H%M%S")
        rel_in_recycle = f"{project_id}/{stamp}-{stem}{suffix}"
        for seq in range(1, 1000):
            dst = self.recycle / rel_in_recycle
            if not dst.exists():
                break
            rel_in_recycle = f"{project_id}/{stamp}-{seq}-{stem}{suffix}"
        else:
            raise OSError(f"回收站命名冲突过多: {project_id}/{stem}{suffix}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return rel_in_recycle

    def purge_recycle(self, keep_days: int | None = None) -> int:
        """清理超过保留期的回收站文件。返回删除个数。"""
        days = self.settings.recycle_retention_days if keep_days is None else keep_days
        cutoff = utcnow() - timedelta(days=days)
        removed = 0
        for p in self.recycle.rglob("*"):
            if not p.is_file():
                continue
            if datetime.fromtimestamp(p.stat().st_mtime, tz=UTC) < cutoff:
                p.unlink(missing_ok=True)
                removed += 1
        return removed

    def usage_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage

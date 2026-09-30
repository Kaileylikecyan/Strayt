"""文件上传（文档 F9）。

两种路径，客户端按 ``size`` 与 ``upload_chunk_threshold`` 自动选择：

1. **直传**（≤ 阈值）   单个 multipart 请求 → 算 SHA-256 → 命中同内容则复用已有 File 行
2. **分块**（> 阈值）   先 ``POST /upload-sessions`` 声明指纹拿 chunk_size，
   再逐块 ``PUT /upload-sessions/{id}/chunks/{index}``，
   最后 ``POST /upload-sessions/{id}/complete`` 合并 + 校验 SHA-256

分块状态记在 ``upload_sessions.received_json``（``{index: size}``），所以中断后
客户端重新 ``GET`` 一次会话就能知道已传哪些块，实现真断点续传。
"""

from __future__ import annotations

import math
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routers.projects import load_project
from app.api.schemas import FileOut, UploadSessionCreate, UploadSessionOut
from app.core.config import get_settings
from app.core.deps import get_db
from app.core.errors import (
    ErrorCode,
    bad_request,
    conflict,
    not_found,
)
from app.db.models import File as FileRow
from app.db.models import UploadSession, new_id, utcnow
from app.storage.local import get_storage, sha256_bytes

router = APIRouter(prefix="/files", tags=["文件"])

_ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md"}
_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2GB 上限，防磁盘打满


def _safe_ext(orig_name: str) -> str:
    ext = PurePosixPath(orig_name).suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise bad_request(ErrorCode.VALIDATION, f"暂不支持的文件类型：{ext or '（无扩展名）'}")
    return ext


def _attach_file(
    db: Session, project_id: str, orig_name: str, rel: str, sha: str, size: int
) -> FileRow:
    """同内容去重：SHA-256 命中就复用磁盘实体，但按项目建独立 File 行。

    ``files`` 的唯一键是 ``(project_id, sha256)``：磁盘上只留一份实体，而同一份
    资料可以挂到多个项目（换个科目再学一遍）。项目内重复上传则直接返回已有行。
    早期版本这里是全局 ``sha256`` 唯一，第二次传同一份资料会拿到**别的项目**的行，
    结果就是建任务时 409「文件不属于该项目」。
    """
    existing = db.scalar(
        select(FileRow).where(FileRow.project_id == project_id, FileRow.sha256 == sha)
    )
    if existing is not None:
        return existing
    f = FileRow(
        id=new_id(),
        project_id=project_id,
        server_path=rel,
        orig_name=orig_name,
        sha256=sha,
        size=size,
    )
    db.add(f)
    return f


@router.get("", response_model=list[FileOut], summary="项目资料列表")
def list_files(project_id: str, db: Session = Depends(get_db)) -> list[FileRow]:
    load_project(db, project_id)
    return list(
        db.scalars(
            select(FileRow)
            .where(FileRow.project_id == project_id, FileRow.deleted_at.is_(None))
            .order_by(FileRow.uploaded_at)
        )
    )


@router.post("", response_model=FileOut, status_code=201, summary="直传上传（≤50MB）")
async def upload_direct(
    project_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> FileRow:
    settings = get_settings()
    if (file.size or 0) > settings.upload_chunk_threshold:
        raise bad_request(
            ErrorCode.VALIDATION,
            f"文件超过 {settings.upload_chunk_threshold // (1024 * 1024)}MB，请走分块上传",
        )

    load_project(db, project_id)
    orig_name = file.filename or "unnamed"
    ext = _safe_ext(orig_name)

    data = await file.read()
    if len(data) > _MAX_BYTES:
        raise bad_request(ErrorCode.VALIDATION, "文件过大")
    if not data:
        raise bad_request(ErrorCode.VALIDATION, "空文件")

    st = get_storage()
    digest = sha256_bytes(data)
    rel = st.alloc_relpath(digest, ext)

    # 同内容已落盘（别的项目先传过）则不重写，直接复用实体
    if not st.abs_path(rel).exists():
        st.write_atomic(rel, data)

    row = _attach_file(db, project_id, orig_name, rel, digest, len(data))
    db.commit()
    db.refresh(row)
    return row


@router.get("/{file_id}/raw", summary="下载原始文件（出处回看二期用）")
def download_raw(file_id: str, db: Session = Depends(get_db)) -> FileResponse:
    f = db.get(FileRow, file_id)
    if f is None or f.deleted_at is not None:
        raise not_found("文件", file_id)
    st = get_storage()
    path = st.abs_path(f.server_path)
    if not path.exists():
        raise not_found("文件实体（磁盘上已不存在）", f.server_path)
    return FileResponse(path, filename=f.orig_name, media_type="application/octet-stream")


# ==========================================================================
# 分块上传 + 断点续传
# ==========================================================================
sess_router = APIRouter(prefix="/upload-sessions", tags=["文件"])


@sess_router.post("", response_model=UploadSessionOut, status_code=201, summary="声明分块上传会话")
def create_session(
    project_id: str,
    body: UploadSessionCreate,
    db: Session = Depends(get_db),
) -> UploadSession:
    load_project(db, project_id)
    if body.size > _MAX_BYTES:
        raise bad_request(ErrorCode.VALIDATION, "文件过大")
    _safe_ext(body.orig_name)

    settings = get_settings()
    chunk_size = settings.upload_chunk_size
    s = UploadSession(
        id=new_id(),
        project_id=project_id,
        orig_name=body.orig_name,
        size=body.size,
        sha256=body.sha256,
        chunk_size=chunk_size,
        total_chunks=math.ceil(body.size / chunk_size),
        received_json={},
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@sess_router.get(
    "/{session_id}", response_model=UploadSessionOut, summary="查询已收分块（续传依据）"
)
def get_session(session_id: str, db: Session = Depends(get_db)) -> UploadSession:
    s = db.get(UploadSession, session_id)
    if s is None:
        raise not_found("上传会话", session_id)
    return s


@sess_router.put(
    "/{session_id}/chunks/{index}",
    status_code=204,
    summary="上传单个分块（可重复调用，幂等覆盖）",
)
async def put_chunk(
    session_id: str, index: int, request: Request, db: Session = Depends(get_db)
) -> None:
    s = db.get(UploadSession, session_id)
    if s is None:
        raise not_found("上传会话", session_id)
    if s.status != "pending":
        raise conflict(ErrorCode.UPLOAD_SESSION_INVALID, f"会话已 {s.status}，不能再收分块")
    if not 0 <= index < s.total_chunks:
        raise bad_request(
            ErrorCode.UPLOAD_CHUNK_OUT_OF_ORDER,
            f"分块序号越界：0~{s.total_chunks - 1}",
        )

    body = bytearray()
    async for part in request.stream():
        body += part
    st = get_storage()
    st.chunk_path(session_id, index).write_bytes(bytes(body))

    s.received_json = {**s.received_json, str(index): len(body)}
    s.updated_at = utcnow()
    db.commit()


@sess_router.post(
    "/{session_id}/complete", response_model=FileOut, summary="合并分块并校验 SHA-256"
)
def complete_session(session_id: str, db: Session = Depends(get_db)) -> FileRow:
    s = db.get(UploadSession, session_id)
    if s is None:
        raise not_found("上传会话", session_id)
    if s.status == "complete":
        row = db.scalar(select(FileRow).where(FileRow.sha256 == s.sha256))
        if row is not None:
            return row
    if s.status != "pending":
        raise conflict(ErrorCode.UPLOAD_SESSION_INVALID, f"会话已 {s.status}")

    received = s.received_json or {}
    missing = [i for i in range(s.total_chunks) if str(i) not in received]
    if missing:
        raise bad_request(
            ErrorCode.UPLOAD_CHUNK_OUT_OF_ORDER,
            f"还缺 {len(missing)} 个分块",
            detail={"missing": missing[:50]},
        )

    st = get_storage()
    parts = [st.chunk_path(session_id, i) for i in range(s.total_chunks)]
    ext = PurePosixPath(s.orig_name).suffix.lower()
    rel = st.alloc_relpath(s.sha256, ext)
    stored = st.concat_chunks(parts, rel)

    if stored.size != s.size:
        raise bad_request(
            ErrorCode.UPLOAD_SIZE_MISMATCH,
            f"大小不符：声明 {s.size}，实得 {stored.size}",
        )
    if stored.sha256 != s.sha256:
        # 内容与声明的指纹不符：删掉合并产物，不入库
        st.abs_path(rel).unlink(missing_ok=True)
        raise bad_request(
            ErrorCode.UPLOAD_CHECKSUM_MISMATCH,
            "SHA-256 校验失败，文件可能上传损坏",
            detail={"expected": s.sha256, "actual": stored.sha256},
        )

    row = _attach_file(db, s.project_id, s.orig_name, rel, stored.sha256, stored.size)
    s.status = "complete"
    s.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    st.drop_session(session_id)
    return row


@sess_router.delete("/{session_id}", status_code=204, summary="放弃会话并清理分片")
def abort_session(session_id: str, db: Session = Depends(get_db)) -> None:
    s = db.get(UploadSession, session_id)
    if s is None:
        raise not_found("上传会话", session_id)
    s.status = "aborted"
    db.commit()
    get_storage().drop_session(session_id)


@router.delete("/{file_id}", status_code=204, summary="软删文件（实体移入回收站）")
def delete_file(file_id: str, db: Session = Depends(get_db)) -> None:
    f = db.get(FileRow, file_id)
    if f is None or f.deleted_at is not None:
        raise not_found("文件", file_id)
    f.deleted_at = utcnow()
    db.commit()
    get_storage().to_recycle(f.server_path, f.project_id)

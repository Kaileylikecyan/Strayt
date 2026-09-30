"""FastAPI 依赖：鉴权、session、设置。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.security import verify_access_token
from app.db.models import Setting
from app.db.session import get_db

# 访问令牌在 settings 表里的键
TOKEN_HASH_KEY = "access_token_hash"

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[Session, Depends(get_db)]


def _extract_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # Tauri 的 WebView 与移动端网页都可能受 CORS 限制拿不到自定义头时，允许 query 兜底
    token = request.query_params.get("token")
    if token:
        return token.strip()
    raise AppError(
        401, ErrorCode.TOKEN_MISSING, "未提供访问令牌，请先在设置中配置服务器地址与访问令牌"
    )


def require_access_token(request: Request, db: DbDep, settings: SettingsDep) -> None:
    """单用户准入：令牌即身份。通过即放行，不挂载任何用户上下文。"""
    token = _extract_token(request)
    row = db.get(Setting, TOKEN_HASH_KEY)
    if row is None or not row.v:
        raise AppError(
            401,
            ErrorCode.TOKEN_INVALID,
            "服务端尚未初始化访问令牌。请在服务端执行 "
            "`uv run python -m app.scripts.gen_token` 生成后填入客户端。",
        )
    if not verify_access_token(token, row.v):
        raise AppError(401, ErrorCode.TOKEN_INVALID, "访问令牌无效，请检查后重新输入")


AuthDep = Depends(require_access_token)

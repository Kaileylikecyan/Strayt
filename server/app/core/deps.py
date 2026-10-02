"""FastAPI 依赖：鉴权、session、设置。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.security import verify_session_token
from app.db.models import Setting
from app.db.session import get_db

# 口令哈希在 settings 表里的键
PASSWORD_HASH_KEY = "access_password_hash"

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[Session, Depends(get_db)]


def _extract_session_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # Tauri 的 WebView 与移动端网页都可能受 CORS 限制拿不到自定义头时，允许 query 兜底
    token = request.query_params.get("token")
    if token:
        return token.strip()
    raise AppError(401, ErrorCode.UNAUTHORIZED, "未登录，请输入访问口令")


def require_session(request: Request, db: DbDep, settings: SettingsDep) -> None:
    """单用户准入：登录换来的会话令牌即身份。通过即放行，不挂载任何用户上下文。

    会话令牌的签名密钥由库里的口令哈希派生，所以这里每次都要把口令哈希读出来验
    签名 —— 顺带得到「改口令立刻踢掉所有旧会话」的行为（ADR-0009）。
    """
    token = _extract_session_token(request)
    row = db.get(Setting, PASSWORD_HASH_KEY)
    if row is None or not row.v:
        raise AppError(
            401,
            ErrorCode.UNAUTHORIZED,
            "服务端尚未设置访问口令。请在客户端首次接入时创建，或在服务端执行 "
            "`uv run python -m app.scripts.set_password`。",
        )
    if not verify_session_token(token, row.v):
        raise AppError(401, ErrorCode.UNAUTHORIZED, "登录已失效或口令错误，请重新登录")


AuthDep = Depends(require_session)

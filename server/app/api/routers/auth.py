"""访问口令登录（需求 F1，单用户免账号）。

流程只有四步，**其中三步免鉴权**：

- ``GET  /auth/state``     探测是否已设置口令 —— 客户端据此决定「创建口令」还是「输入口令」
- ``POST /auth/setup``     创建口令，**仅当尚未设置时可用**（409 拒绝覆盖），免鉴权
- ``POST /auth/login``     口令换会话令牌，免鉴权
- ``POST /auth/password``  改口令，**要带 ``AuthDep``**（见 ``router2``）

为什么改口令要鉴权而创建不要：创建只在「从未设置过」时开放，属于一次性初始化，
此时按定义还没有任何已认证主体可言（单用户系统不存在「第一个管理员」问题）。
改口令是常态操作，必须拿旧口令证明身份。

``POST /auth/setup`` 是这条链路上唯一的「无凭证写操作」。它只把口令哈希写进
``settings`` 一行，不碰任何业务数据；服务端默认只监听回环（AGENTS.md §部署），
所以这不是真实攻击面。真要暴露到局域网，应先在服务端用 ``set_password`` 建好口令。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.schemas import (
    AuthStateOut,
    PasswordChangeIn,
    PasswordIn,
    SessionOut,
)
from app.core.deps import PASSWORD_HASH_KEY, get_db
from app.core.errors import AppError, ErrorCode
from app.core.security import (
    SESSION_TTL_SECONDS,
    hash_password,
    issue_session_token,
    verify_password,
)
from app.db.models import Setting

router = APIRouter(prefix="/auth", tags=["访问口令登录"])

#: 改口令需要已登录。单独一个 router 只是为了能用**不同的** include 依赖挂载：
#: ``main.py`` 里 ``router`` 免鉴权、``router2`` 带 ``AuthDep``。
router2 = APIRouter(prefix="/auth", tags=["访问口令登录"])


def _stored_hash(db: Session) -> str | None:
    row = db.get(Setting, PASSWORD_HASH_KEY)
    return row.v if row and row.v else None


def _write_hash(db: Session, encoded: str) -> None:
    row = db.get(Setting, PASSWORD_HASH_KEY)
    if row is None:
        db.add(Setting(k=PASSWORD_HASH_KEY, v=encoded))
    else:
        row.v = encoded
    db.commit()


def _session_out(password_hash: str) -> SessionOut:
    return SessionOut(
        session_token=issue_session_token(password_hash),
        expires_at=datetime.now(UTC) + timedelta(seconds=SESSION_TTL_SECONDS),
    )


@router.get("/state", response_model=AuthStateOut)
def auth_state(db: Session = Depends(get_db)) -> AuthStateOut:
    """是否已设置口令。免鉴权 —— 客户端首屏就要知道该显示哪个表单。"""
    return AuthStateOut(password_set=_stored_hash(db) is not None)


@router.post("/setup", response_model=SessionOut)
def setup_password(payload: PasswordIn, db: Session = Depends(get_db)) -> SessionOut:
    """首次创建口令。已设置过则 409 —— 不给「静默覆盖」留口子。

    口令强度已由 ``PasswordIn`` 的字段校验器把关（弱口令 → 422），这里
    ``hash_password`` 只会因内部不一致抛错，那确实是 500。
    """
    if _stored_hash(db) is not None:
        raise AppError(
            409,
            ErrorCode.PASSWORD_ALREADY_SET,
            "访问口令已设置。请直接登录；忘记口令可在服务端执行 "
            "`uv run python -m app.scripts.set_password --reset`。",
        )
    encoded = hash_password(payload.password)
    _write_hash(db, encoded)
    return _session_out(encoded)


@router.post("/login", response_model=SessionOut)
def login(payload: PasswordIn, db: Session = Depends(get_db)) -> SessionOut:
    """口令换会话令牌。"""
    stored = _stored_hash(db)
    if stored is None:
        raise AppError(
            409,
            ErrorCode.PASSWORD_NOT_SET,
            "服务端尚未设置访问口令，请在客户端点击「创建口令」，或在服务端执行 "
            "`uv run python -m app.scripts.set_password`。",
        )
    if not verify_password(payload.password, stored):
        raise AppError(401, ErrorCode.PASSWORD_WRONG, "口令错误")
    return _session_out(stored)


@router2.post("/password", response_model=SessionOut)
def change_password(
    payload: PasswordChangeIn,
    db: Session = Depends(get_db),
) -> SessionOut:
    """改口令。鉴权由 ``router2`` 在 ``main.py`` 挂载时用 ``AuthDep`` 施加。

    改完立刻返回**新**会话令牌：签名密钥由口令哈希派生，换口令等于换密钥，
    客户端手上那个旧令牌当场作废。不回传的话用户会被自己踢下线，得重新输一遍。
    """
    stored = _stored_hash(db)
    if stored is None:
        raise AppError(409, ErrorCode.PASSWORD_NOT_SET, "服务端尚未设置访问口令")
    if not verify_password(payload.old_password, stored):
        raise AppError(401, ErrorCode.PASSWORD_WRONG, "当前口令错误")
    # 强度已由 PasswordChangeIn 校验器把关（422）。**顺序很重要**：先验旧口令
    # 再哈希新口令，否则一个不知道旧口令的人可以用弱口令探测「旧口令对不对」。
    encoded = hash_password(payload.new_password)
    _write_hash(db, encoded)
    return _session_out(encoded)

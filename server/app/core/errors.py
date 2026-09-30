"""统一错误体与业务异常。

客户端按 ``error.code`` 做分支（弱同步队列、网络提示、令牌错误），
所以 code 是稳定契约，不要随意改名；message 面向用户，可本地化。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


class ErrorCode:
    """稳定错误码。客户端据此分支。"""

    # 准入
    TOKEN_MISSING = "token_missing"
    TOKEN_INVALID = "token_invalid"
    # 通用
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INTERNAL = "internal"
    # 上传
    UPLOAD_SESSION_INVALID = "upload_session_invalid"
    UPLOAD_CHUNK_OUT_OF_ORDER = "upload_chunk_out_of_order"
    UPLOAD_SIZE_MISMATCH = "upload_size_mismatch"
    UPLOAD_CHECKSUM_MISMATCH = "upload_checksum_mismatch"
    # 加工
    JOB_NOT_RUNNABLE = "job_not_runnable"
    NO_PARSER = "no_parser"
    SCAN_NEEDS_VISION = "scan_needs_vision"
    # 对齐
    ALIGN_EMPTY = "align_empty"
    QUALITY_GATE_FAILED = "quality_gate_failed"
    # 图谱
    GRAPH_NO_DRAFT = "graph_no_draft"
    GRAPH_REJECTED = "graph_rejected"
    # 弱同步
    SYNC_STALE = "sync_stale"


class AppError(HTTPException):
    """带稳定 code 的业务异常。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        detail: Any = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message
        self.extra = detail

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.extra is not None:
            body["error"]["detail"] = self.extra
        return body


def bad_request(code: str, message: str, **kw: Any) -> AppError:
    return AppError(400, code, message, **kw)


def not_found(what: str, ident: Any = None) -> AppError:
    msg = f"{what}不存在" + (f"（{ident}）" if ident is not None else "")
    return AppError(404, ErrorCode.NOT_FOUND, msg)


def conflict(code: str, message: str, **kw: Any) -> AppError:
    return AppError(409, code, message, **kw)


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_body())


async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    """把 Starlette/FastAPI 自带 HTTPException 也收敛到同一错误体形状。"""
    if isinstance(exc, AppError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())
    code = {
        400: ErrorCode.VALIDATION,
        401: ErrorCode.TOKEN_INVALID,
        403: ErrorCode.TOKEN_INVALID,
        404: ErrorCode.NOT_FOUND,
        409: ErrorCode.CONFLICT,
        422: ErrorCode.VALIDATION,
    }.get(exc.status_code, ErrorCode.INTERNAL)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": str(exc.detail)}},
    )

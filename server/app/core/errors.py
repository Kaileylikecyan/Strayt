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
    UNAUTHORIZED = "unauthorized"
    PASSWORD_NOT_SET = "password_not_set"
    PASSWORD_ALREADY_SET = "password_already_set"
    PASSWORD_WRONG = "password_wrong"
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
        401: ErrorCode.UNAUTHORIZED,
        403: ErrorCode.UNAUTHORIZED,
        404: ErrorCode.NOT_FOUND,
        409: ErrorCode.CONFLICT,
        422: ErrorCode.VALIDATION,
    }.get(exc.status_code, ErrorCode.INTERNAL)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": str(exc.detail)}},
    )


def jsonable_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 pydantic 的 ``exc.errors()`` 洗成能直接 ``json.dumps`` 的结构。

    必须洗：``Field(min_length=...)`` 这类约束失败时 ``ctx`` 里是数字，序列化的动；
    但**自定义校验器抛的 ``ValueError`` 会被 pydantic 原样塞进 ``ctx["error"]``**，
    那是活的异常对象，``json.dumps`` 直接 ``TypeError``。症状很难认：
    请求体非法本该回 422，实际却是 500，而且日志里只有一句
    ``Object of type ValueError is not JSON serializable``。

    所以这里逐层递归：dict / list / tuple 展开，其余非 JSON 原生类型退化成字符串。
    校验器作者不用为这件事操心。
    """
    return [_plain(v) for v in errors]


def _plain(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_plain(x) for x in v]
    return str(v)

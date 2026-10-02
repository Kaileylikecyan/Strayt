"""FastAPI 应用入口。

分层：
- ``app.main``      应用装配、异常处理、生命周期
- ``app.core.*``     配置 / 安全 / 错误 / 依赖
- ``app.api.*``      路由（只做参数校验与编排，业务逻辑下沉到对应模块）
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routers import auth as auth_router_mod
from app.api.routers import export as export_router_mod
from app.api.routers import files as files_router_mod
from app.api.routers import graph as graph_router_mod
from app.api.routers import jobs as jobs_router_mod
from app.api.routers import plans as plans_router_mod
from app.api.routers import projects as projects_router_mod
from app.api.routers import recite as recite_router_mod
from app.api.routers import settings as settings_router_mod
from app.api.routers import sync as sync_router_mod
from app.core.config import get_settings
from app.core.deps import AuthDep
from app.core.errors import (
    AppError,
    ErrorCode,
    app_error_handler,
    http_error_handler,
    jsonable_errors,
)
from app.jobs.engine import recover_orphaned_jobs, run_manager

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_settings().ensure_dirs()
    # 把主事件循环交给 RunManager：``/jobs`` 是同步路由（FastAPI 丢到线程池跑），
    # 那边没有运行中的 loop，``asyncio.create_task`` 会 RuntimeError（500）。
    run_manager.bind_loop(asyncio.get_running_loop())
    # 重启会把后台协程全丢了（RunManager 的取舍）。把那些没人在跑的任务落成
    # interrupted，客户端就能显示「续跑」而不是永远转圈。
    recover_orphaned_jobs()
    yield
    # 关窗前把后台任务收干净：正在跑的任务要么等它跑到单元边界，要么让
    # checkpoint 停在原处（下次 resume 接着跑），别留一堆悬空协程。
    await run_manager.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=(
            "学习工作台 Strayt 服务端。\n\n"
            "单用户免账号：所有 `/api/v1` 端点需 `Authorization: Bearer <访问令牌>`。\n"
            "唯一豁免是 `/health`（存活探针）。\n\n"
            '错误体统一为 `{ "error": { "code", "message", "detail?" } }`，'
            "`code` 是稳定契约，客户端据此分支。"
        ),
        lifespan=lifespan,
    )

    # 移动端网页走同源部署时不需要 CORS；这里放开是为了开发期 Vite 跨端口调试。
    #
    # Tauri 的 origin 分平台，**三个都要写上**，少一个桌面端就 `failed to fetch`：
    #   - Windows   → `http://tauri.localhost`（WebView2 用 http 模拟自定义协议以满足同源策略）
    #   - macOS/Linux → `tauri://localhost`
    #   - devUrl    → `http://127.0.0.1:5174`（已被上面的 localhost/127.0.0.1 分支覆盖）
    # 只写 `tauri://localhost` 会让 Windows 桌面端完全连不上服务端 —— 而报错只发生在
    # 客户端侧（`failed to fetch`），服务端日志干干净净，极易误判成"服务没起"。
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?"
        r"|tauri://localhost"
        r"|http://tauri\.localhost)$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_req, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": ErrorCode.VALIDATION,
                    "message": "请求参数不合法",
                    # 必须过 jsonable_errors：自定义校验器抛的 ValueError 会留在
                    # errors() 的 ctx 里，直接序列化会 500 而不是回 422
                    "detail": jsonable_errors(exc.errors()),
                }
            },
        )

    @app.get("/health", tags=["运维"], summary="存活探针（免鉴权）")
    def health() -> dict:
        return {
            "ok": True,
            "app": settings.app_name,
            "server_name": settings.server_name,
            "schema_version": settings.snapshot_schema_version,
        }

    # 准入本身免鉴权（否则「还没登录」就无法发请求登录）。
    # `/auth/password` 例外 —— 它挂在 router2 上并单独带 AuthDep，因为改口令必须
    # 证明身份。参见 app/api/routers/auth.py 的模块说明。
    app.include_router(auth_router_mod.router, prefix=API_PREFIX)
    app.include_router(auth_router_mod.router2, prefix=API_PREFIX, dependencies=[AuthDep])

    # 鉴权挂在 include_router 层而不是各路由的 dependencies 上：
    # 新增路由只要 include 进来就自动受保护，不可能「忘了加」。
    # /health 是 app 级路由，不经过这里，天然豁免。
    for r in (
        projects_router_mod.router,
        files_router_mod.router,
        files_router_mod.sess_router,
        settings_router_mod.router,
        jobs_router_mod.router,
        graph_router_mod.router,
        graph_router_mod.router2,
        graph_router_mod.router3,
        graph_router_mod.router4,
        recite_router_mod.router,
        sync_router_mod.router,
        plans_router_mod.router,
        export_router_mod.router,
    ):
        app.include_router(r, prefix=API_PREFIX, dependencies=[AuthDep])

    return app


app = create_app()

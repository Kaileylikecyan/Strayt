# ADR-0008 加工任务绑主事件循环调度，启动时回收孤儿任务

- 状态：已采纳
- 日期：2026-09-29
- 关联：`app/jobs/engine.py`（`RunManager`、`recover_orphaned_jobs`）、
  `app/main.py`（lifespan）、`app/api/routers/jobs.py`、
  `tests/test_api_jobs.py::test_should_bind_loop_and_start_from_sync_route`、
  `tests/test_api_jobs.py::test_should_mark_orphaned_running_job_interrupted`

## 背景

AGENTS.md §3 红线 7：加工任务在服务端后台跑，客户端可以关窗。但「后台跑」这件事在
FastAPI + SQLAlchemy 同步 session 的组合下有两个坑，真实数据联调时才暴露：

1. **路由是同步的，事件循环不在这一层。** `POST /jobs` / `/jobs/{id}/resume` 写成
   `def`（为了用同步 `Session`），FastAPI 把它们丢进线程池执行；`RunManager.submit`
   在里面调 `asyncio.create_task` → `RuntimeError: no running event loop` → 整个请求 500。
   单测里 `run_manager` 被换成了 fake，`create_task` 那一行从没真跑过。
2. **进程重启后 `running` 的任务永远停在那。** 任务状态落库（红线 7：支持断点续跑），
   但没有回收机制的话，上次崩溃/被 Ctrl-C 的任务会永远占着 `running`，客户端既看不到
   进度也点不了续跑（`resume` 只接受 `failed/cancelled/interrupted`）。

## 决策

1. **`RunManager` 显式持有主事件循环**：`lifespan` 里 `bind_loop(asyncio.get_running_loop())`。
2. **同步上下文里提交任务走 `loop.call_soon_threadsafe(self._start, ...)`**，由 loop 线程
   真正建 engine 和 `create_task`。`resume` 同一条路径。这样"谁调用的"不再重要 ——
   同步路由、测试、将来的 CLI 入口都能提交任务。
3. **启动时回收孤儿任务**：`recover_orphaned_jobs()` 把 `running`/`queued` 且
   `heartbeat_at` 早于当前时刻的旧任务置为 `interrupted`，并把 `error` 写成
   「服务重启中断，续跑将从断点继续」。`interrupted` 正好是 F8 成本熔断用的那个终态，
   客户端已有渲染分支，不用为「重启中断」新增状态。
4. **回收判据用 `heartbeat_at`**，不无条件打断：进程刚起、任务还没来得及写心跳的情况
   由 `run_manager` 的内存态兜底（同一进程内不会有遗留），DB 里判据只看时间戳。

## 理由

- 让"能不能起任务"取决于有没有 loop，而不取决于调用栈在哪一层，是唯一能同时满足
  同步 DB session 和 asyncio 后台执行的形状。
- 复用 `interrupted` 而不是加第七个状态：这个状态的客户端语义本来就是"停下来了，
  你决定要不要花钱继续"，重启中断正好落在这个语义里，用户点一下续跑就接着跑。

## 后果

- `jobs.status` 的 `interrupted` 现在有两个来源：成本熔断（ADR 记录在 F8）与重启中断。
  `error` 文本区分两者，客户端只需展示并提供续跑。
- 测试锚点两个：真实 `RunManager` 走同步路由不 500（起一个后台 loop 线程再打请求），
  以及启动 lifespan 时遗留 `running` 被改成 `interrupted`。
- **别把路由改回 `async def` 来"修"这个 500**：那会把同步 `Session` 拿到 loop 上，
  换来的是并发下的 session 复用问题，比 500 难查得多。

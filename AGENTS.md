# AGENTS.md · 学习工作台 Strayt

> 单人自用学习项目工作台。瘦客户端（Tauri / 响应式网页）+ 胖服务端（FastAPI + MySQL 8 + 文件存储 + LLM 调用层）。
> 产品依据：`docs/学习工作台产品说明文档.md`。任务依据：仓库根目录的实施规划（Phase 0~5）。

---

## 1. 本机环境陷阱（踩过就别再踩）

| 陷阱 | 现象 | 正确做法 |
|---|---|---|
| **PowerShell 拦截 `npm`** | 跑 `npm` 报 `PSSecurityException`（`npm.ps1` 被执行策略禁） | 用 `npm.cmd`，或一次性 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| **控制台 GBK 乱码** | 中文输出全花屏 | 跑 Python 脚本前设 `$env:PYTHONUTF8=1`；或 `chcp 65001` |
| **`git` 仓库曾错建在家目录** | `C:\Users\10698\.git`（零提交）会试图纳管整个用户目录 | 仓库根必须是 `C:\Users\10698\Desktop\study-studio`，用 `git rev-parse --show-toplevel` 自检 |
| **Anaconda base 污染** | base 里已有 fastapi/SQLAlchemy，但缺 pypdf 等 | 一律用 `uv` 在 `server/.venv` 里操作，**禁止** `pip install` 到 base |
| **原型遗留：pip 曾完全不可用** | 旧记忆说清华源 `from versions: none` | 已过期。现 `https://pypi.tuna.tsinghua.edu.cn/simple/` 实测可用 |
| **`.ps1` 被执行策略拦** | 跑 `.\scripts\check.ps1` 报 `PSSecurityException` | `powershell -ExecutionPolicy Bypass -File .\scripts\check.ps1`（单次，不改策略），或 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| **PS 5.1 把无 BOM 的 `.ps1` 按 GBK 解析** | 脚本报"表达式或语句中包含意外的标记"，中文全花屏 | `.ps1` **必须存成带 BOM 的 UTF-8**。用 `[IO.File]::WriteAllText($p, $t, (New-Object Text.UTF8Encoding $true))`，别用 Edit 工具直接写 |
| **PS 5.1 的 `*>` 会死锁** | `.\scripts\check.ps1 *> log` 跑到某一步永远不返回（原生命令同时大量写 stdout+stderr 时，alembic 就会） | 用文件重定向：<br>`Start-Process powershell -ArgumentList '-File','.\scripts\check.ps1' -RedirectStandardOutput log -NoNewWindow` |
| **工具写 stderr 被当成失败** | `$ErrorActionPreference='Stop'` 时，alembic 的 `INFO` 行变 `NativeCommandError`，退出码 0 却报红 | 判成败只看 `$LASTEXITCODE`；调外部命令期间把 `ErrorActionPreference` 降回 `Continue`（`check.ps1` 的 `Invoke-Step` 已这么做） |

### 已具备 / 缺失

- ✅ WebView2 Runtime 154.0.4258.37（Tauri 前置已满足）
- ✅ Node v24.21.0 / uv 0.12.19 / Python 3.12.7 / Docker 27.3.1（引擎需手动启动）/ WSL2
- ❌ **Rust / cargo 未装**、❌ **MSVC Build Tools + Windows SDK 未装** → Tauri 构建前必须补齐（见 §4）
- 依赖下载走镜像：npm → `registry.npmmirror.com`，cargo → rsproxy，pip → 清华源（uv 已配）

---

## 2. 常用命令

```powershell
# 服务端
cd server
uv sync                                  # 装依赖
uv run alembic upgrade head              # 迁移
uv run uvicorn app.main:app --reload --port 8000
uv run python -m app.scripts.gen_token   # 生成首个访问令牌
uv run pytest
uv run ruff check . && uv run ruff format .

# 前端
cd apps\desktop ; npm.cmd run tauri dev
cd apps\web     ; npm.cmd run dev
cd apps\desktop ; npm.cmd run check      # lint + typecheck + test

# 全量自检（lint + 格式 + 迁移 + 契约 + 文档链接 + 测试 + 真实数据 E2E）
.\scripts\check.ps1
.\scripts\check.ps1 -Fast          # 跳过两个真实数据 E2E（约 65s）
.\scripts\check.ps1 -SkipTests     # 只跑静态检查与契约
# 报 PSSecurityException 见 §1；想存日志见 §1 的 `*>` 死锁那行

# 改了服务端接口后重新导出契约（check.ps1 会校验有没有过期）
uv run --project server python scripts\export_openapi.py

# HTTP 级真实数据 E2E（对着已跑着的服务端走客户端那条路）
# 令牌别写进文件：设环境变量
$env:STRAYT_TOKEN = "<访问令牌>"
uv run --project server python scripts\smoke_http.py     # 跑完自动删临时项目
uv run --project server python scripts\smoke_http.py --keep   # 留着进网页端看
```

> `check.ps1` 会对每个步骤计时并在末尾汇总失败项，退出码 0/1 可直接接 CI。
> 但它跑的是**进程内**的验证；「同步路由 + 后台调度」「跨项目同内容上传」
> 「带时区 `client_ts`」这三类问题它抓不到（单测也是进程内）。改完任务引擎或
> 上传/同步接口，**手跑一次 `scripts\smoke_http.py`**（见 `docs/rtm.md` 已知缺口 9）。

---

## 3. 架构红线（违反即回滚）

1. **客户端不碰数据库**，一切经服务端 REST API（含文件上传/下载）。
2. **解析与 LLM 调用只在服务端**。客户端只做：拉取、交互展示、进度标记。
3. **原始文件存服务端磁盘**，DB 存相对路径 + SHA-256；同内容去重。
4. **API Key 存服务端**（混淆/密文），客户端配置时上送。界面必须明示"Key 将存储在你的服务器上"。
5. **访问令牌哈希存储**；单用户免账号，令牌是唯一准入。
6. **弱同步**：打开全量拉取 → 覆盖 snapshot；写操作直传优先、失败入 `write_queue`；联网按 `client_ts` 顺序重放；冲突以 `updated_at` 时间戳裁决。
7. **加工任务在服务端后台跑**，客户端可关窗；状态落 `jobs` 表，支持断点续跑。
8. **`loc_page`（页号）必须在一期就保留**。出处回看是二期/三期能力，页号丢了只能重跑全部加工。

---

## 4. 双主题规则（文档 §8.2 硬性要求）

两套完整 UI 风格：**现代简约风** / **像素风**。样式资源**完全隔离，禁止混写**。

- 技术实现：Ant Design `ConfigProvider theme` token（现代简约 = antd 默认），
  像素风 = 独立 token 覆盖 + 独立 CSS 覆盖层（`packages/tokens/src/pixel.css`）。
  两文件互不 import，物理隔离。
- 组件层（`packages/ui`）**只读 token，禁止字面量**颜色 / 间距 / 圆角。
- 一键切换、即时生效（无刷新）、偏好存本地。
- 门禁：`npm run check:tokens` 扫描 `apps/` 与 `packages/ui`，命中字面量即失败，纳入 CI。
- 图表/图谱调色板（含节点/边/掌握度三色）**必须来自 token，不得硬编码在 JS 里**。
- **CSS 变量用 kebab-case，token 键用 camelCase**：`tokensToCssVars` 输出
  `--tok-spaceMd` / `--tok-colorPrimary`（不是 `--tok-space-md`）。
  数字型 token 同时输出一个带 `-px` 的变体（`--tok-spaceMd-px`），
  **长度类样式一律用 `-px` 那个**（`var(--tok-spaceMd-px)`），别写
  `calc(var(--tok-spaceMd) * 1px)` —— `* 1px` 是字面量，门禁会拦。

---

## 5. 目录约定

```
server/          FastAPI 服务端
  app/core/      配置、鉴权、错误体
  app/db/        SQLAlchemy 模型与 session
  app/models/    领域模型（与 ORM 解耦处）
  app/api/       路由
  app/storage/   磁盘文件存储、SHA-256、回收站
  app/parse/     解析器（pdf/docx/text/扫描检测/分段）
  app/align/     对齐双通道（regular 直配 / llm 语义对齐）
  app/llm/       LLMProvider 抽象与 6 家实现、牌价
  app/jobs/      加工任务引擎、checkpoint
  app/graph/     图谱管线（LLM 抽取 agent、跨篇合并与编辑、入库、掌握度递进）
apps/desktop/    Tauri v2 + React（电脑端，上传 + 完整学习）
apps/web/        响应式网页（项目/资料上传/加工任务/图谱视图/闪卡/背诵舱/打卡/设置）
packages/tokens/ 双主题 Design Token
packages/ui/     组件层（只读 token）
packages/api-client/  由 openapi.yaml 生成的类型 + 请求封装
packages/sync-engine/ 弱同步引擎
docs/adr/        架构决策记录（0001~0008，正文列 LONGTEXT / 稳定 key / 同事务 / 续跑状态 / 质量门控 / 图谱草稿与擦除重建 / 文件去重边界 / 任务调度与孤儿回收）
docs/api/        OpenAPI 契约（生成物，由 scripts/export_openapi.py 产出）
docs/rtm.md      需求追踪矩阵（F1~F24 ↔ 实现 ↔ 测试 ↔ 状态）
docs/README.md   文档索引
```

---

## 6. 加工管线不变量

```
PDF ─→ ①版面提取(pypdf layout) ─→ ②页级清洗 ─→ ③块结构识别 ─→ ④语言判别
     ─→ ⑤段落切分 ─→ ⑥对齐(A:LLM / B:直配) ─→ ⑦质量自检 ─→ ⑧入库
```

- ①**必须**用 `extraction_mode="layout"`。样板 PDF 零空行全硬换行，plain 模式拿不到段落边界。
  `layout` 会依 y 距离与字高推断空行（pypdf 6.19 已验证支持）。
- ②**禁止硬编码页数**（原型 `共 50 页` 写死是已修 bug）。
- ③ 结构正则**外置为可配置规则**，不同资料版式不同。
- ④ 语言判别用 `zh_ratio < 0.3`；用高精度终止符（`谢谢大家！` / `Thank you all!`）辅助定位块尾。
- ⑥ 段落级对齐，**禁止按句索引配对**（样板实测中英句数不等：49 vs 55 / 48 vs 56，按索引必然错位）。
- ⑦ 入库前必过 CJK 比例自检（中文字段 ≈100% 中文、英文字段 ≈0%），任一篇不过则拦下不入库。

---

## 7. 禁止事项

- ❌ 账号体系 / 注册登录 / 多用户多租户（个人自用，访问令牌即可）
- ❌ 通用笔记编辑器（只做资料 → 结构化数据的单向加工 + 有限手动编辑）
- ❌ 社交 / 社区 / 内容推荐 / 资料的在线搜索下载
- ❌ 移动原生 App（移动端走响应式网页）
- ❌ 遥测、广告位、面向未成年人的社交或推荐
- ❌ 卡片内容超出原文范围（LLM 提示词强制"仅基于给定材料"，无依据处生成占位而非编造）

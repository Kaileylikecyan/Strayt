# 需求追踪矩阵（F1~F24）

需求编号与优先级来自产品说明文档 §6。本表记录每条需求当前的落地情况。

## 图例

| 标记 | 含义 |
|---|---|
| ✅ | 服务端已完成，有测试覆盖 |
| 🟡 | 服务端部分完成，或仅服务端完成、客户端未做 |
| ⛔ | 未开始 |
| 🚫 | 本期明确不做 |

> **当前整体状态**：服务端（`server/`）覆盖 F1、F4、F6、F7、F9、F10、F17、F20、F21
> 的后端部分。**网页端（`apps/web`）已跑通主链路**：项目列表（含最近学习）、资料上传
> （直传/分块续传）、加工任务（创建/轮询/取消/续跑/成本熔断展示）、背诵舱四档 + 手动改句、
> 图谱（预览确认 / 图谱视图 / 闪卡 / 掌握度）、打卡热力、设置（API Key/模型档案/导出）、
> 弱同步引擎 10 个单测。
> **Tauri 桌面端（`apps/desktop`）仍空**：Rust/cargo 与 MSVC Build Tools 未装，
> 等工具链补齐再接（共享包已就绪，补 UI 即可）。
>
> **背景**：初始迁移已经把图谱型的 `categories` / `nodes` / `edges` / `card_quotes` /
> `flashcards` 五张表建好，`plans` / `goals` / `checkins` 也有表和写入通道；
> 图谱服务端（抽取/合并/入库/接口）与网页端视图均已实现，唯一缺口是真实文档的
> 端到端验证（需配置 LLM Key）。

## 测试分布（`server/tests/`，共 525 个测试）

> 数字取自 `uv run pytest --collect-only -q`，别手改 —— 改完跑一次刷新。

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_align.py` | 47 | 通道 B 排版直配 |
| `test_align_llm.py` | 55 | 通道 A LLM 对齐 |
| `test_align_quality.py` | 32 | ⑦ 质量门控 |
| `test_api_jobs.py` | 26 | 任务 API、真实 RunManager 调度、孤儿任务回收 |
| `test_auth.py` | 41 | 口令认证四层（哈希/签名/过期/HTTP）+ CORS origin 精确性 |
| `test_category_rules.py` | 22 | F12 分类规则 CRUD、优先级语义、非法正则挡回 |
| `test_export.py` | 3 | 全量快照导出（口令哈希必须被剔除） |
| `test_files.py` | 7 | 上传与文件管理（含跨项目同内容去重） |
| `test_jobs.py` | 52 | 任务引擎、checkpoint、取消、续跑 |
| `test_llm.py` | 41 | 12 家 provider、牌价、自定义端点不可覆盖固定地址 |
| `test_pairs_edit.py` | 8 | F18 拆分/合并、`loc_page` 继承、`seq` 稠密唯一 |
| `test_parse.py` | 45 | ①~⑤ 解析管线 |
| `test_persist.py` | 19 | ⑧ 入库、拒收、人工微调保护 |
| `test_plans.py` | 6 | 计划读取聚合 |
| `test_projects.py` | 5 | 项目 CRUD、目标 |
| `test_settings_api.py` | 19 | Provider 契约、自定义端点边界、KV 不得改写口令哈希 |
| `test_sync.py` | 11 | 弱同步批量重放、冲突裁决、`pair_key` 寻址、带时区 `client_ts` |
| 图谱族（`test_graph_*.py`、`test_api_graph.py`、`test_api_mastery.py`、`test_api_flashcards.py`） | 86 | 抽取/合并/入库/掌握度/闪卡 |

真实数据验证：
- `server/var_test/quality_e2e.py`（⑥⑦，12 篇 / 634 对）、`server/var_test/job_e2e.py`（③④⑤⑥⑦⑧ + 任务引擎，6 场景）—— 进程内跑引擎。
- `scripts/smoke_http.py`（**协议面**）—— 对着跑着的服务端走客户端那条路：鉴权 → bootstrap →
  上传 → 建任务 → 轮询到 `success` → 校验篇目/对句/`loc_page` → F18 `pair_edit`（`pair_key` 寻址）
  → 旧 `client_ts` 重放判 `conflict` → F18 `split`/`merge`（行数守恒、`seq` 稠密唯一、`loc_page` 继承、
  不相邻拒绝合并）。加 `--graph` 额外跑 F12：分类规则 CRUD（整表覆盖 / 空白与非法正则挡回 / 可清空）
  与 `node_category` 写实体三道闸（未知 node、未知分类、非白名单字段）。需口令（`STRAYT_PASSWORD`，脚本自己走 `/auth/login` 换会话令牌）
  与在跑的服务端，跑完自动清理临时项目。单测抓不到、只能靠它抓的 bug 见 ADR-0007 / ADR-0008
  与缺口 3（本轮 `autoflush` 漏 flush）。

客户端单测：`packages/sync-engine/tests/engine.test.ts`（vitest，10 用例：入队持久化、
client_ts 偏移、按时间排序重放、LWW 冲突移除、error 保留、分页、断网保留、快照覆盖）。
门禁：`scripts/check-tokens.mjs`（`apps/` 与 `packages/ui` 禁样式字面量）+ 根 `npm run typecheck`。

---

## 6.1 接入与同步

| ID | 需求 | 状态 | 实现 | 测试 |
|---|---|---|---|---|
| F1 | 服务端接入配置（地址 + 访问口令登录） | ✅ | **口令登录**（ADR-0009）：服务端 `app/core/security.py`（Argon2id 哈希 + HMAC 会话令牌）、`app/core/deps.py`（`require_session`）、`app/api/routers/auth.py`（`/auth/state` `/setup` `/login` `/password`）、`app/scripts/set_password.py`（忘记口令的 CLI 逃生口）；客户端 `apps/web` Setup 页**双态**（首次创建口令 / 之后输入口令，由 `/auth/state` 决定）+ 设置页「修改口令」+ `src/config.ts` 只存地址与会话令牌 | `test_auth.py`（33 例：哈希/签名/过期/HTTP 四层）；`scripts/smoke_http.py` 走 `/auth/login` 换令牌 |
| F2 | 弱同步：全量拉取 / 离线读 / 离线写队列 / 冲突按 `updated_at` 裁决 | ✅ | `GET /sync/bootstrap`、`POST /sync/batch`；客户端 `packages/sync-engine`（快照覆盖/写队列/flush 按 client_ts+LWW）+ `apps/web` store `save()`（直传优先、失败入队）。`client_ts` 在服务端统一折成 naive UTC（浏览器 `toISOString()` 带 `Z`，与库里的 naive 比较会 TypeError） | `test_sync.py`（11 用例）；`packages/sync-engine` `tests/engine.test.ts` 10 用例 |
| F3 | 同步状态角标 | ✅ | 客户端 `packages/ui` `SyncBadge`（synced/syncing/offline+待传数）+ `apps/web` Shell 顶栏 | — |

## 6.2 项目管理器

| ID | 需求 | 状态 | 实现 | 测试 |
|---|---|---|---|---|
| F4 | 创建/删除/重命名项目，选类型 | ✅ | `app/api/routers/projects.py`；类型创建后不可改；删除级联 | `test_projects.py` |
| F5 | 项目列表：类型图标、最近学习、进度概览 | ✅ | `GET /projects` 概览字段；客户端 `apps/web` Projects 卡片：类型图标 `ProjectTypeTag`、篇目进度、**最近学习**（`dayjs` 相对时间，取已学篇目最新 `updated_at`）、创建日期；建项时可选背诵/图谱类型 | `test_projects.py`；web 冒烟已验 |

## 6.3 设置中心

| ID | 需求 | 状态 | 实现 | 测试 |
|---|---|---|---|---|
| F6 | API Key 管理（12 家）+ 连通测试 | ✅ | `app/api/routers/settings.py` + `api_keys` 表（加 `base_url` 列）+ `app/llm/registry.py`（**注册表为唯一真源**，`Provider = Literal[tuple(PROVIDERS)]` 派生，见 ADR-0011）；国内 8 家（DeepSeek / 千问 / 智谱 / Kimi / 豆包 / SiliconFlow / MiniMax / 混元）+ 海外 3 家 + `openai_compatible` 自建网关。Key 存服务端；`/probe` 连通测试。**固定端点的厂商填 `base_url` 会被拒**（`Authorization` 跟着地址走，注册表写死的地址是信任边界） | `test_llm.py`（`TestProviderRegistry`）、`test_settings_api.py`（`TestProviderLiteral` / `TestCustomEndpoint`） |
| F7 | 按项目选服务商与模型，文本/视觉分开 | ✅ | `model_profiles` 表 + `/settings/model-profiles`（CRUD）；客户端 `apps/web` 设置页：档案列表/新建/删除，文本与视觉模型分两个输入 | `test_llm.py`；web 冒烟已验 |
| F8 | 加工成本预估（按牌价，标"估算"） | ✅ | 服务端：`app/llm/pricing.py` 牌价 + `estimate_cost`；引擎首跑把整篇预估计入 `cost_estimate_json.estimated_cny`，每篇落库后比对，实际超预估 1.5 倍即停在 `interrupted` 终态等确认，续跑即视为同意继续。客户端：任务卡展示预估（规则通道显式标"不花钱"）、进度、熔断原因；**续跑前弹确认框**（摆出预估与断点，`JobsPage::confirmResume`） | `test_jobs.py` 费用用例 + `Test成本熔断`；`scripts/smoke_http.py` 通道 B 预估 ¥0 |

## 6.4 资料上传

| ID | 需求 | 状态 | 实现 | 测试 |
|---|---|---|---|---|
| F9 | 上传文件/文件夹、分块续传、通道标注 | ✅ | `app/api/routers/files.py` + `upload_sessions` 四端点；SHA-256 指纹去重（**按项目**去重，见 ADR-0007）；原始文件存服务端磁盘。客户端 `apps/web` `upload.ts` + FilesPage：≤50MB 直传、较大文件浏览器端算 SHA-256 后分块上传并跳过已传块、进度回调、通道标注展示 | `test_files.py`（7 用例，含跨项目同内容）；`scripts/smoke_http.py` |
| F10 | 上传管理、删除文件（级联提示） | ✅ | `GET/DELETE /files`、`GET /files/{id}/raw`；客户端 FilesPage 文件列表（大小/通道/页数）与删除，删除前确认影响加工任务 | `test_files.py` |

## 6.5 知识图谱型项目

> `categories` / `nodes` / `edges` / `card_quotes` / `flashcards` 五张表已建，
> `nodes.mastery` 有 `no/mid/yes` 约束 —— 依赖 LLM，**没有降级通道**，
> 没有配置 LLM 时建图任务直接 409（`ErrorCode.VALIDATION`）。

| ID | 需求 | 状态 | 实现 | 测试 |
|---|---|---|---|---|
| F11 | 图谱加工：解析 → LLM 抽取 → 预览确认 → 入库 | 🟡 | **服务端已完成**：`app/graph/extract.py`（Agent 抽取）→ `merge.py`（跨篇合并）→ 草稿落 `jobs.results_json` → `POST /jobs/{id}/graph-confirm` 确认后 `persist.py` 原子入库（同事务擦除重建）；`GET /projects/{id}/graph` 全量取图；**客户端 `apps/web` GraphPage「图谱」tab**：最近成功 `graph_extract` 任务的草稿预览（删节点 / 调分类 / 改名 / 改概要）→ `graph-confirm` 提交 | `test_graph_extract.py`、`test_graph_merge.py`、`test_graph_persist.py`、`test_graph_jobs.py`、`test_api_graph.py` |
| F12 | 一级分类（LLM 划分 / 自定义规则 / 手动调整） | ✅ | 抽取 prompt 内建分类划分 + `merge.py` 跨篇同名折叠；多余空分类自动修剪；**自定义规则**：`category_rules` 表 + 迁移，`app/graph/classify.py` 按「文件名 / 篇章标题 × 前缀 / 包含 / 正则」匹配，入库前（`jobs/graph.py::_finish` → `merge_units`）取**第一条命中**整篇归类，未命中保持 LLM 原判；CRUD `GET`/`PUT /projects/{id}/category-rules`（`PUT` 整表覆盖，**数组下标即优先级**，落库为 `priority` 列 —— 早期版本按随机 `id` 升序排，语义上是错的，见迁移 `d4f1a8b6c207`），入口拒空白 pattern / 非法正则；**手动调整**：预览里 `apply_edits` 的 `reassign` 下拉 + 图谱视图**拖节点换列**（`node_category` 弱同步写实体，含跨项目分类挡回） | `test_category_rules.py`、`test_graph_merge.py`、`scripts/smoke_http.py --graph` |
| F13 | 图谱视图（分区着色、搜索高亮、掌握度三色） | 🟡 | 数据（节点/边/掌握度）随 `GET /projects/{id}/graph` 返回；客户端 `apps/web` GraphPage：按一级分类分列、列头/SVG 元素取 `categoryCssVar` 调色板、搜索高亮（`focusOutline`）、掌握度三色描边 + 左侧色条（`masteryNo/Mid/Yes` token）、点击节点抽屉看概要/出处/改掌握度 | — |
| F14 | 知识卡片（概要 + 摘录 + 出处回看） | 🟡 | 卡片数据（`summary`/`quote` 含 `file_id`+`loc_page`+`para`）已随 graph API 返回；**卡片渲染与出处回看（二期）为客户端**（节点抽屉已展示概要 + 摘录 + 页码） | `test_graph_persist.py` |
| F15 | 闪卡自测 | 🟡 | **服务端已完成**：`POST /projects/{id}/flashcards/generate`（确定性重建：正面=节点名，背面=概要/摘录，无答案跳过）、`GET /projects/{id}/flashcards?mastery=`、`POST /flashcards/{id}/result`（经 `app/graph/progress.py::next_mastery` 回写掌握度）；**客户端 `apps/web` GraphPage「闪卡」tab**：按掌握度筛选、重建卡组、翻面答题、对/错回传、一轮统计与再来一轮 | `test_api_flashcards.py`、`test_graph_progress.py` |
| F16 | 掌握度统计 | 🟡 | **服务端已完成**：`nodes.mastery` 三态 `no/mid/yes`；`GET /projects/{id}/mastery-stats`（总体+分类聚合）、`PUT /nodes/{id}/mastery`、弱同步实体 `node_mastery`（写回 + LWW）；**客户端 `apps/web` GraphPage**：统计行（未掌握/掌握中/已掌握/掌握率）、节点抽屉改掌握度、分类进度条，三色取 token。**写路径走 `store.save()` 弱同步**：在线 `PUT /nodes/{id}/mastery` 直传，离线入 `node_mastery` 写队列 + LWW 重放（闪卡答题同样：在线走 `/flashcards/{id}/result` 权威推进，离线降级为客户端 `next_mastery` + `node_mastery` 队列） | `test_api_mastery.py`、`test_graph_progress.py`、`packages/sync-engine/tests/engine.test.ts`（`node_mastery` 离线入队/在线重放） |

## 6.6 熟读背诵型项目

| ID | 需求 | 状态 | 实现 | 测试 |
|---|---|---|---|---|
| F17 | 加工执行：解析 → 中英对齐 → 入库 | ✅ | `app/jobs/engine.py` 全链路；`POST /jobs`、`/resume`；后台调度见 ADR-0008（绑主 loop + 启动回收孤儿任务）；扫描件显式 `NotImplementedError`。客户端 `apps/web` JobsPage：按项目/类型建任务、2.5s 轮询进度、取消、失败/中断续跑、显示加工单元与失败单元 | `test_jobs.py`、`test_api_jobs.py`、**`var_test/job_e2e.py`**、`scripts/smoke_http.py` |
| F18 | 中英对齐双通道 + 手动微调 | ✅ | 通道 A `app/align/llm.py`、通道 B `app/align/regular.py`，自动选择已接；**写入通道**：弱同步 `pair_edit`，**按稳定 key `pair_key` 寻址**（客户端拿不到自增主键，服务端 `db.get` 落空时按 `pair_key` 兜底），可改 `zh`/`en`/`seq`，带 `manually_edited`；**拆分 / 合并**：`POST /pieces/{id}/pairs/split`（客户端算好两半，服务端换新 `pair_key` + 继承 `loc_page`）与 `/merge`（仅相邻，中文直拼英文空格连），两者都经 `_renumber_pairs` 把 `seq` 压回 0..n-1 稠密唯一；客户端 RecitePage 改句弹窗 + 复读确认 + **HTML5 拖拽排序** + 拆分/合并弹窗 | `test_align_llm.py`、`test_align.py`、`test_pairs_edit.py`、`test_sync.py::test_pair_edit_accepts_stable_pair_key`、`var_test/quality_e2e.py`、`scripts/smoke_http.py` |
| F19 | 交错背诵舱四档模式 | ✅ | 服务端已提供段落对序列（`GET /pieces`、`/pairs`）；客户端 `apps/web` RecitePage 实现四档（对照阅读/中文提示/遮罩背诵/逐句递进）+ 字号调节 + `last_pos`/`recited` 落库 + 手动改句 | — |
| F20 | 篇目管理：已背诵标记、进度 x/n、上次位置 | ✅ | `pieces.recited` / `last_pos`；`PUT /pieces/{id}/progress`；`sync` 的 `piece_progress` | `test_persist.py` |

## 6.7 学习数据与进度

| ID | 需求 | 状态 | 实现 | 测试 |
|---|---|---|---|---|
| F21 | 每项目独立进度存服务端 | ✅ | `pieces.recited`/`last_pos`、`nodes.mastery`；`POST /sync/batch` 重放 | `test_persist.py`、`test_sync.py` |
| F22 | 学习路径闭环（目标 → 计划 → 待办 → 完成） | ✅ | `goals` / `plans` 表 + `sync` 的 `goal`/`plan` 写通道；**读取聚合已做**：`GET /plans`（阶段计划列表，逾期优先排序 + `days_until`/`overdue`）、`GET /plans/daily`（每日待办分桶）；客户端 `apps/web` CheckinPage「今天待办」完成按钮（`plan` 入队/直传）+ Shell `SyncBadge` | `test_projects.py`、`test_plans.py` |
| F23 | 打卡与热力图 | ✅ | `checkins` 表 + `PUT /checkins` + `GET /checkins/{day}`；客户端 `apps/web` CheckinPage（`checkin_items` kv 默认项 + 保存 + 连续天数 + `HeatCell` 30 天五档热力，颜色取 token） | `test_sync.py` |
| F24 | 数据导出（JSON 快照） | ✅ | `GET /export/snapshot` 全量 JSON 快照：业务表 + 已软删项目 + 模型档案 + 密文 Key + 设置（令牌哈希），覆盖 F22 闭环与图谱表；客户端 `apps/web` 设置页「导出全量数据」按钮下载 JSON | `test_export.py` |

## 6.8 本期不做

| 事项 | 状态 |
|---|---|
| 账号体系 / 多用户、社交推荐、遥测、移动原生 App | 🚫 AGENTS.md §7 列为禁止事项 |
| `vision` 扫描件 OCR 通道 | 🚫 一期显式 `NotImplementedError`（F9/F17 的扫描件路径） |

---

## 加工管线不变量（AGENTS.md §6）

| 阶段 | 实现 | 测试 | 真实数据 |
|---|---|---|---|
| ① 版面提取（必须 `layout` 模式） | `app/parse/pdf.py` | `test_parse.py` | ✅ |
| ② 页级清洗 | `app/parse/clean.py` | `test_parse.py` | ✅ |
| ③ 块结构识别（正则外置可配置） | `app/parse/blocks.py` | `test_parse.py` | ✅ |
| ④ 语言判别（`zh_ratio < 0.3`） | `app/parse/lang.py` | `test_parse.py` | ✅ |
| ⑤ 段落切分 | `app/parse/blocks.py` | `test_parse.py` | ✅ |
| ⑥ 对齐（段落级，双通道） | `app/align/` | 102 | ✅ 12 篇 / 634 对 |
| ⑦ 质量自检 | `app/align/quality.py` | 32 | ✅ |
| ⑧ 入库（原子 + 拒收不写） | `app/persist.py` | 19 | ✅ |

## 架构决策

见 `docs/adr/`：

| ADR | 决策 |
|---|---|
| [0001](adr/0001-longtext-for-body-columns.md) | 正文列用 LONGTEXT，不靠应用层截断 |
| [0002](adr/0002-stable-unit-key-for-checkpoint.md) | 加工单元用 `index:title` 稳定 key，不拿标题当身份 |
| [0003](adr/0003-piece-and-checkpoint-same-transaction.md) | 篇目写入与 checkpoint 同事务提交 |
| [0004](adr/0004-preserve-unit-status-on-resume.md) | 续跑从 checkpoint 恢复单元状态，不重置为 pending |
| [0005](adr/0005-quality-gate-blocks-persistence.md) | 质量门控在入库前拦，拒收也要记账 |
| [0006](adr/0006-graph-draft-in-results-json-and-wipe-rebuild.md) | 图谱草稿存 `results_json`，确认后同一事务擦除重建 |
| [0007](adr/0007-file-dedup-scope-per-project.md) | 文件去重以 `(project_id, sha256)` 为界，磁盘实体仍按内容寻址复用 |
| [0008](adr/0008-bind-main-loop-and-recover-orphan-jobs.md) | RunManager 绑主事件循环调度，启动时把遗留任务回收为 `interrupted` |
| [0009](adr/0009-password-login-and-hash-derived-session-token.md) | 口令登录 + 会话令牌由口令哈希派生（无会话表） |
| [0010](adr/0010-category-rule-priority-column.md) | 分类规则优先级由显式 `priority` 列承载，不用随机 `id` |
| [0011](adr/0011-provider-registry-single-source-and-custom-endpoint.md) | 服务商注册表为唯一真源；自定义端点仅 `openai_compatible` 开放 |

## 已知缺口（下一阶段的实际入口）

1. ~~Tauri 桌面端：脚手架与前端已完成，编译卡在 Windows SDK~~ —— **已出 exe 并验证启动**。
   `apps/desktop` 原本是三个空目录，现已补齐：
   - **前端**：`src/main.tsx` 只做挂载，页面全部 import 自 `@strayt/web/*`
     （vite alias + tsconfig paths 指到 `apps/web/src`）。**同源，不复制页面** ——
     `npm run build -w @strayt/web` 与 `-w @strayt/desktop` 产出的 JS 资源 hash 完全相同
     （`index-Gat2oVwU.js`，945.39 kB），已验证三次。改动 `apps/web` 桌面端自动同步。
   - **原生层**：`src-tauri/{Cargo.toml,build.rs,tauri.conf.json,capabilities/default.json,src/{main,lib}.rs}`
     + 图标。`lib.rs` 刻意**零 plugin、零业务逻辑**（AGENTS.md §3 红线：解析与 LLM 只在服务端），
     权限只给 `core:default`。窗口 1280×820，devUrl `127.0.0.1:5174`。
   - **产出**：`target\release\strayt-desktop.exe`，3.22 MB，`ProductName: Strayt`、
     `FileVersion: 0.1.0`。**实测启动成功**：窗口标题「学习工作台 Strayt」，
     1293×856（配置 1280×820 + 系统边框），27.6 MB 内存 / 26 线程，stderr 干净。
    - **服务端**：`main.py` 的 CORS `allow_origin_regex` **同时**含 `http://tauri.localhost`
      与 `tauri://localhost`，实测两个 origin 都会回显，而 `evil.example.com` 与
      `http://tauri.localhost.evil.com` 不回（后者是本轮修掉的真 bug —— Windows 上
      WebView2 走的是 `http://tauri.localhost` 而非 `tauri://localhost`，只放行后者时
      桌面端所有请求都是 `failed to fetch`，但网页端 dev 完全正常，很容易误判成前端问题）。

   - **验证**：`npm run check:frontend`（check:tokens → 全仓 typecheck → test → build）全绿；
     `cargo check` 退出码 0 无警告。
   - **遗留**：**MSI/NSIS 安装包打不出来** —— `tauri build` 要从 GitHub 下 WiX 工具链
     （`wixtoolset/wix3` release），本机到 GitHub 的连接时断时续，会报
     `failed to bundle project: Peer disconnected`。**exe 本身不受影响**（Rust 编译已完成），
     要装包时网络能通 GitHub 重跑即可；只想验证编译用
     `npm run tauri:build -w @strayt/desktop -- --no-bundle`（退出码 0，已验证）。
- **已解决**：首次启动停在 Setup 页的障碍已解除。原实现要手输令牌，而令牌在
      `settings.access_token_hash` 里存的是**加盐 PBKDF2**、拿不回明文（§3 红线 5，故意如此），
      所以自动化测试填不进去。现在改为**口令**：库中无口令时 Setup 页直接创建，之后输口令登录
      （ADR-0009）。口令同样只存 Argon2id 哈希，但创建/登录这条 HTTP 路径本身可自动化，
      `scripts/smoke_http.py` 已改为走 `/auth/login` 换会话。
2. **图谱型管线服务端已完成，客户端非空但缺真实数据验证** —— F11~F16 的服务端抽取/合并/入库/接口
   （`app/graph/`、`app/jobs/graph.py`、`app/api/routers/graph.py`）已就绪；网页端 GraphPage「图谱」tab
   已实现预览确认、图谱视图（双主题调色板 F13）、闪卡自测（F15）、掌握度统计（F16），空态/契约形状已
   用临时图谱项目核对（`GET /graph`、/mastery-stats、/flashcards、generate）。**受无 LLM Key 限制，
   未用真实文档跑通抽取 → 草稿 → 确认的端到端链路**（配置 Key 后可用 `scripts/smoke_http.py` 扩展）。
3. ~~F18 只做了一半~~ —— 已补齐：服务端 `POST /pieces/{id}/pairs/split` 与 `/merge`
   （F12/F18 本轮一起做的），客户端 RecitePage 拖拽排序 + 拆分/合并弹窗。
   桌面端因与网页端同源（见缺口 1）自动就有了这套 UI。
   本轮修掉一个真 bug：`SessionLocal` 是 `autoflush=False`，拆分/合并新加的行在
   `_renumber_pairs` 的 SELECT 前没 flush，导致返回列表漏行且新行带着旧 `seq` 与重编号后的
   行**撞 seq**（客户端排序错位）。同时把 `tests/conftest.py` 的 sessionmaker 改成与生产
   逐项对齐（原来 autoflush 默认 True，把这类 bug 全遮住了 —— 见缺口 10）。
4. ~~F8 的"预估与实际差异 >50% 中断"~~ —— 已实现：引擎首跑把整篇预估计入
   `cost_estimate_json.estimated_cny`，每篇落库后比对，超 1.5 倍停在 `interrupted` 终态
   （`b39d0fcb295f` 给 `jobs.status` 加了该值），续跑视为确认继续并关闭熔断。
   网页端展示 `interrupted` 的原因，**续跑前有费用确认弹窗**（F8 闭环）。
   遗留：`interrupted` 现在也用于「服务重启中断」（ADR-0008），两种原因靠 `error` 文本区分，
   客户端文案没细分，但都有续跑入口。
5. ~~客户端未接 F22/F24 UI~~ —— 已做：网页端 CheckinPage 待办完成按钮 + 设置页导出按钮。
6. **F13 依赖双主题 Design Token**（AGENTS.md §4 的硬性要求，样式资源必须物理隔离）。
   `packages/tokens` 双主题 + `packages/ui` 组件层已就绪、`check:tokens` 门禁已通；
   图谱视图已用 `chartCategory` 调色板 + 掌握度三色 token 渲染。
   `check:tokens` 扫的是整个 `apps/`，桌面端页面因与网页端同源**自动同门禁**
   （`src-tauri/target|gen|icons` 已加进 `IGNORE_DIRS`，否则会递归扫爆）。
7. **`app/models/` 是空文件** —— AGENTS.md §5 规划的"领域模型（与 ORM 解耦）"未使用，
   目前 ORM 直接当领域模型用。
8. **本轮成果未提交** —— 仓库只有一个提交（`52b0f9d` 初版服务端、网页端），
   本轮 F12/F18/F8/F22/F24 与整个 `apps/desktop` 仍是工作区改动，**没有版本保护**。
   投产前先提交一次。
9. **协议面没有自动化门禁** —— `server/tests/` 全是进程内调用（TestClient 之外不碰 socket），
   「同步路由 + 后台调度」「跨项目同内容上传」「带时区 `client_ts`」这三类问题单测抓不到，
   都是 `scripts/smoke_http.py` 手工跑出来的（ADR-0007 / ADR-0008）。它要活服务端 + 令牌，
   CI 跑不了，所以**改完接口或任务引擎后要手跑一次**（见 AGENTS.md §2）。
10. **测试夹具与生产 session 配置漂移过一次** —— `tests/conftest.py` 原来写
    `sessionmaker(bind=engine)`（autoflush 默认 True），而 `app/db/session.py` 是
    `autoflush=False`。后果是「先 `add` 再 `select` 查不到新行」这类 bug 单测全绿、
    只在 HTTP 层炸（本轮 split/merge 的 seq 撞车就是这样漏出去的）。已改成
    `sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)` 逐项对齐，
    并在注释里写明「生产 session 配置变了这里要一起改」。**再引入新的 session 级配置
    时记得同步**。
11. **校验器抛 `ValueError` 会把 422 变成 500** —— pydantic 把自定义校验器抛的异常对象
    原样留在 `errors()["ctx"]` 里，直接丢给 `JSONResponse` 序列化必炸。本轮加
    `app/core/errors.py::jsonable_errors` 在 422 处理器里过一道（`Field(min_length=...)`
    这类约束失败不受影响，所以以前没暴露）。**新写校验器不用为此操心**，但别绕过它
    直接用 `exc.errors()`。

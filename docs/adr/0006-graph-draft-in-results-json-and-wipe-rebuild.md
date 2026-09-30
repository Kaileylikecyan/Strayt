# ADR-0006 图谱草稿存 results_json，确认后同一事务擦除重建

- 状态：已采纳
- 日期：2026-09-28
- 关联：`app/jobs/graph.py`（`GraphEngine`、`_settle`）、`app/graph/persist.py`（`save_graph`）、
  `app/api/routers/graph.py`（`graph-confirm`）

## 背景

图谱加工与背诵加工有一个关键差异：背诵是"逐篇可独立提交"（每篇一个事务），而图谱的
产物是**跨篇的关系全图**，客户端在确认前要看到整图的草稿（所有单元合并结果），还可能
做有限编辑。产物了两层：

- **草稿**：LLM 抽取 + 跨篇合并后的 `GraphDraft`，等客户端预览/编辑/确认。
- **正式图**：确认后写入 `categories` / `nodes` / `edges` / `card_quotes`。

草稿去哪、正式图怎么覆盖旧图，两个问题都要一次定死，否则重跑/重新确认会产生孤儿数据
或旧节点残留。

## 决策

1. **草稿复用 `jobs.results_json`**，不再开新存储。结构：
   `{"units": {篇key: GraphUnit→dict}, "draft": GraphDraft→dict, "status": "pending_confirm" | "confirmed"}`。
   `GraphUnit.to_json()` 带 `index`，`results_json` 只要存在就按它恢复单元，保证续跑后
    `_finish` 仍能重建与首次完全一致的草稿（`list_pending_job_ids` 因此把 `graph_extract`
    与 `recite_align` 并列处理）。
2. **结果写入与 checkpoint 同事务**：`_settle` 在同一个已开着的 `Session` 里写
   `results_json` 和 `checkpoint_json`（沿用 ADR-0003），`GraphEngine` 单元循环内一律走
   构建在 Session 上的 `_commit`，不另开事务。
3. **确认采用"整图擦除重建"语义**：`save_graph` 先 DELETE 该项目下
   `card_quotes` → `edges` → `nodes` → `categories`，再全量 INSERT，全程同一事务由
   graph-confirm 统一提交。返回 `REPLACED`（覆盖了旧图）或 `REJECTED`（草稿为空 / 缺
   文件出处时拒收，不写任何行）。
4. **节点 key 与分类 key 必须稳定可复算**：节点 `u{###}:n{##}`（篇内序号），分类
   `c{###}`（按空白折叠后的名称去重分配）；不然 wipe-and-rebuild 之后 key 漂移，客户端
   编辑白做了。
5. **编辑只是改草稿，不改正式图**：`apply_edits` 在 `GraphDraft` 上施加编辑（改名触发
   `edited` 标记、空名即删、200 字截断、`reassign` 只接受现存分类 key、删节点连带删边），
   只有确认（`POST .../graph-confirm`）才把编辑结果落库。

## 理由

- 图谱一贯要"看全图再动手"，草稿必须先于任何入库；而正式图又是不可增量的整体（分类
  会合并、节点会改名），一条条 diff 去改是脆的。擦除重建 + 同事务是最简单的原子语义。
- 把 results 和 checkpoint 放同一事务，性质同 ADR-0003：崩在中间最多丢进度重跑，不会
  出现"库里半张图 + checkpoint 说没做"的分裂。

## 后果

- `save_graph` 一次事务里可能出现多次 DELETE + INSERT；测试锚定：
  `test_should_wipe_old_graph_on_reconfirm`（覆盖旧图）、`test_should_reject_empty_draft`
  （拒收不写库）、`test_should_reject_when_file_missing`（`card_quotes.file_id` NOT NULL
  的提前拦截）。
- 确认在 `dogma` 事务内抛错时整单回滚，客户端看到事务性失败而非半图。